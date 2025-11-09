from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple

import pandas as pd

from ynabbridge.account_mapping import get_or_create_mapping
from ynabbridge.formatter import convert_to_ynab_format

LOGGER = logging.getLogger(__name__)


class OwnerBudgetStore(Protocol):
    """Protocol describing the subset of EmailStateStore used by the ingestion engine."""

    def get_owner_budget(self, owner: str) -> Optional[str]:
        ...

    def set_owner_budget(self, owner: str, budget_id: str) -> None:
        ...


@dataclass
class IngestionItem:
    """Normalized ingestion input fed into the shared engine."""

    item_id: str
    display_name: str
    dataframe: pd.DataFrame
    fallback_owner: str
    source: str
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class SourceCallbacks:
    """Optional hooks for ingestion sources."""

    on_success: Optional[Callable[[IngestionItem], None]] = None
    on_failure: Optional[Callable[[IngestionItem, str], None]] = None


@dataclass
class IngestionResult:
    """Result of running uploads through the shared engine."""

    successful_items: List[IngestionItem]
    failed_items: List[Tuple[IngestionItem, str]]

    @property
    def all_succeeded(self) -> bool:
        return not self.failed_items


def select_budget_interactive(budgets: List[dict], current_id: Optional[str]) -> Optional[str]:
    """Prompt the user to select a budget from YNAB budgets."""
    if not budgets:
        return None
    default_idx = None
    if current_id:
        for i, budget in enumerate(budgets):
            if budget.get('id') == current_id:
                default_idx = i
                break
    print("Select YNAB budget:")
    for idx, budget in enumerate(budgets):
        marker = "*" if default_idx is not None and idx == default_idx else " "
        print(f"  [{idx}] {marker} {budget.get('name')} ({budget.get('id')})")
    prompt = "Enter number"
    if default_idx is not None:
        prompt += f" [default {default_idx}]"
    prompt += ": "
    while True:
        choice = input(prompt).strip()
        if choice == "" and default_idx is not None:
            return budgets[default_idx]['id']
        if choice.isdigit():
            idx = int(choice)
            if 0 <= idx < len(budgets):
                return budgets[idx]['id']
        print("Invalid selection, try again.")


class IngestionEngine:
    """Shared ingestion orchestrator for local files, email attachments, and future sources."""

    def __init__(
            self,
            ynab_client,
            budgets: List[dict],
            state_store: Optional[OwnerBudgetStore],
            default_budget_id: Optional[str],
            default_account_id: Optional[str],
            force_budget_id: Optional[str] = None,
    ):
        self.ynab_client = ynab_client
        self.budgets = budgets
        self.state_store = state_store
        self.default_budget_id = default_budget_id
        self.default_account_id = default_account_id
        self.force_budget_id = force_budget_id

        self._owner_budget_cache: Dict[str, str] = {}
        self._budget_accounts_cache: Dict[str, List[dict]] = {}
        self._owner_summary: Dict[str, Dict[str, object]] = {}
        self._owner_batches: Dict[str, Dict[str, object]] = {}
        self._owner_item_map: Dict[str, Set[str]] = {}
        self._item_owner_map: Dict[str, Set[str]] = {}
        self._item_registry: Dict[str, IngestionItem] = {}
        self._item_callbacks: Dict[str, SourceCallbacks] = {}
        self._item_failures: Dict[str, str] = {}
        self._flushed = False
        self._result: Optional[IngestionResult] = None

    def add_items(self, items: Iterable[IngestionItem], callbacks: Optional[SourceCallbacks] = None) -> None:
        """Add parsed ingestion items to the shared pipeline."""
        if items is None:
            return
        for item in items:
            self._item_registry[item.item_id] = item
            if callbacks:
                self._item_callbacks[item.item_id] = callbacks
            self._process_item(item)

    def record_source_warning(
            self,
            label: str,
            display_name: str,
            metadata: Optional[Dict[str, object]],
            warning: str,
    ) -> None:
        """Record a warning for a source that could not be parsed/processed."""
        owner_key = _owner_cache_key(label) or label
        entry = self._owner_summary.setdefault(owner_key, _init_owner_summary_entry(label))
        entry['label'] = label
        entry['sources'].add(display_name)
        if metadata:
            for uid in metadata.get('message_uids', set()) or []:
                entry['messages'].add(uid)
        entry['skipped'] += 1
        entry['warnings'].append(warning)

    def flush(self) -> IngestionResult:
        """Upload prepared transactions and trigger callbacks."""
        if self._flushed:
            return self._result or IngestionResult([], [])

        successful_owner_keys: Set[str] = set()
        for owner_key, batch in self._owner_batches.items():
            transactions = batch['transactions']
            entry = self._owner_summary[owner_key]
            if not transactions:
                entry['warnings'].append("No transactions prepared for upload.")
                continue
            budget_id = batch['budget_id']
            LOGGER.info(
                "Uploading %d transactions for owner %s to budget %s.",
                len(transactions),
                batch['label'],
                budget_id,
            )
            success = self.ynab_client.upload_transactions(transactions, budget_id)
            if success:
                entry['uploaded'] += len(transactions)
                successful_owner_keys.add(owner_key)
            else:
                entry['warnings'].append("YNAB upload failed.")
                for item_id in self._owner_item_map.get(owner_key, set()):
                    self._item_failures[item_id] = "YNAB upload failed."

        successful_items: List[IngestionItem] = []
        failed_items: List[Tuple[IngestionItem, str]] = []
        for item_id, item in self._item_registry.items():
            if item_id in self._item_failures:
                failed_items.append((item, self._item_failures[item_id]))
                continue
            owner_keys = self._item_owner_map.get(item_id, set())
            if owner_keys and all(owner_key in successful_owner_keys for owner_key in owner_keys):
                successful_items.append(item)
            else:
                reason = "Owner batch failed."
                failed_items.append((item, reason))
                self._item_failures[item_id] = reason

        for item in successful_items:
            callbacks = self._item_callbacks.get(item.item_id)
            if callbacks and callbacks.on_success:
                callbacks.on_success(item)

        for item, reason in failed_items:
            callbacks = self._item_callbacks.get(item.item_id)
            if callbacks and callbacks.on_failure:
                callbacks.on_failure(item, reason)

        self._flushed = True
        self._result = IngestionResult(successful_items, failed_items)
        return self._result

    @property
    def has_items(self) -> bool:
        return bool(self._item_registry)

    @property
    def summary(self) -> Dict[str, Dict[str, object]]:
        return self._owner_summary

    def print_summary(self, title: str) -> None:
        _print_owner_summary(self._owner_summary, title)

    def _process_item(self, item: IngestionItem) -> None:
        owner_groups = _group_df_by_owner(item.dataframe, item.fallback_owner)
        prepared_batches: List[Tuple[str, str, str, List[dict]]] = []

        for owner_label, owner_df in owner_groups:
            owner_key = _owner_cache_key(owner_label) or owner_label
            entry = self._owner_summary.setdefault(owner_key, _init_owner_summary_entry(owner_label))
            entry['label'] = owner_label
            entry['sources'].add(item.display_name)
            for uid in item.metadata.get('message_uids', set()) or []:
                entry['messages'].add(uid)
            entry['parsed'] += 1

            budget_id = self._resolve_budget_for_owner(owner_label)
            if not budget_id:
                warning = f"Missing budget mapping for {item.display_name}"
                entry['warnings'].append(warning)
                entry['skipped'] += 1
                self._item_failures[item.item_id] = warning
                return

            accounts = self._get_accounts_for_budget(budget_id)
            if not accounts and not self.default_account_id:
                warning = (
                    f"No YNAB accounts available for budget {budget_id}. "
                    "Set YNAB_ACCOUNT_ID or create an account."
                )
                entry['warnings'].append(warning)
                entry['skipped'] += 1
                self._item_failures[item.item_id] = warning
                return

            name_map = _build_account_mapping(owner_df, budget_id, accounts, self.state_store)
            if 'account_name' in owner_df.columns:
                missing_names = [
                    nm for nm in {str(n).strip() for n in owner_df['account_name'].dropna().unique()}
                    if nm.strip().lower() not in name_map and not self.default_account_id
                ]
                if missing_names:
                    warning = f"Missing account mapping for {', '.join(missing_names)}"
                    entry['warnings'].append(warning)
                    entry['skipped'] += 1
                    self._item_failures[item.item_id] = warning
                    return

            transactions = convert_to_ynab_format(
                owner_df,
                self.default_account_id,
                name_map if name_map else None,
            )
            if not transactions:
                warning = f"No transactions ready after formatting: {item.display_name}"
                entry['warnings'].append(warning)
                entry['skipped'] += 1
                self._item_failures[item.item_id] = warning
                return

            prepared_batches.append((owner_key, owner_label, budget_id, transactions))

        for owner_key, owner_label, budget_id, transactions in prepared_batches:
            batch = self._owner_batches.setdefault(
                owner_key,
                {'label': owner_label, 'budget_id': budget_id, 'transactions': []},
            )
            batch['transactions'].extend(transactions)
            self._owner_summary[owner_key]['prepared'] += len(transactions)
            self._owner_item_map.setdefault(owner_key, set()).add(item.item_id)
            self._item_owner_map.setdefault(item.item_id, set()).add(owner_key)

    def _get_accounts_for_budget(self, budget_id: str) -> List[dict]:
        accounts = self._budget_accounts_cache.get(budget_id)
        if accounts is None:
            accounts = self.ynab_client.list_accounts(budget_id)
            self._budget_accounts_cache[budget_id] = accounts or []
            if not accounts:
                LOGGER.warning("No accounts retrieved for budget %s.", budget_id)
        return accounts

    def _resolve_budget_for_owner(self, owner_label: str) -> Optional[str]:
        key = _owner_cache_key(owner_label) or owner_label.lower()
        if self.force_budget_id:
            self._owner_budget_cache[key] = self.force_budget_id
            return self.force_budget_id
        if key in self._owner_budget_cache:
            return self._owner_budget_cache[key]
        stored = self.state_store.get_owner_budget(owner_label) if self.state_store else None
        if stored:
            self._owner_budget_cache[key] = stored
            return stored
        selected = _prompt_budget_for_owner(owner_label, self.budgets, self.default_budget_id)
        if selected and self.state_store:
            self.state_store.set_owner_budget(owner_label, selected)
        if selected:
            self._owner_budget_cache[key] = selected
        return selected


def _clean_owner_label(value: object) -> str:
    if value is None:
        return ''
    text = str(value).strip()
    if not text or text.lower() in {'nan', 'none', 'null'}:
        return ''
    return text


def _owner_cache_key(owner_label: str) -> str:
    return _clean_owner_label(owner_label).lower()


def _group_df_by_owner(df: pd.DataFrame, fallback_label: str) -> List[Tuple[str, pd.DataFrame]]:
    if 'owner_name' not in df.columns:
        return [(fallback_label, df)]
    owner_series = df['owner_name'].apply(_clean_owner_label)
    valid_mask = owner_series != ''
    if not valid_mask.any():
        return [(fallback_label, df)]
    owners = sorted(owner_series[valid_mask].unique())
    groups: List[Tuple[str, pd.DataFrame]] = []
    for owner in owners:
        owner_df = df.loc[owner_series == owner].copy()
        groups.append((owner, owner_df))
    return groups


def _prompt_budget_for_owner(
        owner_label: str,
        budgets: List[dict],
        default_budget_id: Optional[str],
) -> Optional[str]:
    print(f"\nOwner '{owner_label}' has no budget mapping.")
    selected = select_budget_interactive(budgets, default_budget_id)
    if not selected:
        LOGGER.warning("No budget selected for owner %s. Transactions will be skipped.", owner_label)
    return selected


def _build_account_mapping(
        df: pd.DataFrame,
        budget_id: str,
        accounts: Sequence[dict],
        state_store: Optional[OwnerBudgetStore],
) -> Dict[str, str]:
    """Prompt for account mapping per unique `account_name`."""
    mapping: Dict[str, str] = {}
    if not accounts or 'account_name' not in df.columns:
        return mapping
    unique_names = sorted(
        {str(name).strip() for name in df['account_name'].dropna().unique() if str(name).strip()}
    )
    for name in unique_names:
        mapped_id = get_or_create_mapping(budget_id, name, accounts, store=state_store)
        if mapped_id:
            mapping[name.strip().lower()] = mapped_id
    return mapping


def _init_owner_summary_entry(label: str) -> Dict[str, object]:
    return {
        'label': label,
        'sources': set(),
        'messages': set(),
        'parsed': 0,
        'prepared': 0,
        'uploaded': 0,
        'skipped': 0,
        'warnings': [],
    }


def _print_owner_summary(summary: Dict[str, Dict[str, object]], title: str) -> None:
    if not summary:
        LOGGER.info("%s finished with no work to report.", title)
        return
    print(f"\n{title}")
    for data in summary.values():
        label = data.get('label')
        attachments = len(data.get('sources', set()))
        parsed = data.get('parsed', 0)
        prepared = data.get('prepared', 0)
        uploaded = data.get('uploaded', 0)
        skipped = data.get('skipped', 0)
        print(
            f"- {label}: files={attachments}, parsed={parsed}, prepared={prepared}, "
            f"uploaded={uploaded}, skipped={skipped}"
        )
        warnings = data.get('warnings', [])
        if warnings:
            for warn in warnings:
                print(f"    ! {warn}")
