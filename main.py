from __future__ import annotations

import argparse
import io
import logging
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from config import load_config
from importers.email_importer import (
    EmailStateStore,
    _log_recent_senders,
    connect_email,
    download_attachments,
)
from importers.local_importer import archive_last_month, process_local_files
from processors.alipay_processor import parse_alipay_csv, process_alipay
from processors.wechat_processor import process_wechat
from ynabbridge.account_mapping import get_or_create_mapping
from ynabbridge.formatter import convert_to_ynab_format
from ynabbridge.ynab_client import YNABClient

logger = logging.getLogger(__name__)


def detect_platform(filename: str) -> str:
    """Detect payment platform from filename."""
    fname = filename.lower()
    if 'alipay' in fname or '支付宝' in fname:
        return 'alipay'
    if 'wechat' in fname or '微信' in fname:
        return 'wechat'
    return 'unknown'


def _extract_alipay_owner(content: bytes) -> Optional[str]:
    """Extract owner identifier from Alipay CSV header if present."""
    encodings = ['utf-8', 'gbk', 'gb18030']
    for enc in encodings:
        try:
            text = content.decode(enc)
        except UnicodeDecodeError:
            continue
        for line in text.splitlines():
            if '支付宝账户' in line:
                _, _, tail = line.partition('支付宝账户')
                tail = tail.lstrip('：:').strip()
                return tail or None
    return None


def _extract_wechat_owner(content: bytes) -> Optional[str]:
    """Extract owner identifier from WeChat export header if present."""
    try:
        header_df = pd.read_excel(io.BytesIO(content), header=None, nrows=6)
    except Exception:
        return None
    for value in header_df.fillna('').astype(str).values.flatten():
        if isinstance(value, str) and '微信昵称' in value:
            # Typically formatted as 微信昵称：[name]
            part = value.split('：', 1)[-1].strip()
            part = part.strip('[]')
            return part or None
    return None


def process_transaction_file(filename: str, content: bytes) -> Optional[pd.DataFrame]:
    """Process a single transaction file and return standardized DataFrame, or None."""
    platform = detect_platform(filename)
    logger.info("Detected platform '%s' for file: %s", platform, filename)
    if platform == 'alipay':
        logger.info("Using Alipay parser for file: %s", filename)
        df = parse_alipay_csv(content)
        if df is None or getattr(df, 'empty', False):
            logger.error("parse_alipay_csv returned empty/None for file: %s", filename)
            return None
        logger.debug("Alipay CSV parsed: shape=%s, columns=%s", df.shape, list(df.columns))
        processed_df = process_alipay(df)
        if processed_df is None or getattr(processed_df, 'empty', False):
            logger.warning("process_alipay produced empty/None for file: %s", filename)
            return None
        logger.debug("Alipay processed: shape=%s, columns=%s", processed_df.shape, list(processed_df.columns))
        owner_label = _extract_alipay_owner(content)
        if owner_label:
            processed_df['owner_name'] = owner_label
        return processed_df
    if platform == 'wechat':
        logger.info("Using WeChat parser for file: %s", filename)
        lower_name = filename.lower()
        if lower_name.endswith(('.xlsx', '.xls')):
            try:
                df = pd.read_excel(io.BytesIO(content))
                logger.debug("WeChat Excel read: shape=%s", df.shape)
            except Exception as exc:
                logger.error("Failed to read WeChat Excel %s: %s", filename, exc)
                return None
        else:
            encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030']
            for encoding in encodings:
                try:
                    content_str = content.decode(encoding)
                    df = pd.read_csv(io.StringIO(content_str))
                    logger.debug("WeChat CSV read with encoding=%s, shape=%s", encoding, df.shape)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                logger.error("Failed to decode CSV: %s", filename)
                return None
        processed_df = process_wechat(df)
        owner_label = _extract_wechat_owner(content)
        if owner_label is not None:
            processed_df['owner_name'] = owner_label
        return processed_df

    # Fallback for generic CSV/Excel, but block Alipay files by header check
    lower_name = filename.lower()
    if lower_name.endswith('.csv'):
        encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030']
        for encoding in encodings:
            try:
                content_str = content.decode(encoding)
                if '支付宝交易记录明细' in content_str or '支付宝' in content_str[:100]:
                    logger.error("Detected Alipay CSV structure in generic CSV logic for file: %s. Aborting parse.",
                                 filename)
                    return None
                df = pd.read_csv(io.StringIO(content_str))
                logger.debug("Generic CSV read with encoding=%s, shape=%s", encoding, df.shape)
                break
            except UnicodeDecodeError:
                continue
        else:
            logger.error("Failed to decode CSV: %s", filename)
            return None
    else:
        try:
            df = pd.read_excel(io.BytesIO(content))
            logger.debug("Excel read: shape=%s", df.shape)
        except Exception as exc:
            logger.error("Failed to read Excel %s: %s", filename, exc)
            return None
    df.columns = df.columns.str.strip().str.lower()
    return df


def _select_budget_interactive(budgets: List[dict], current_id: Optional[str]) -> Optional[str]:
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


def _match_budget_identifier(budgets: List[dict], identifier: str) -> Optional[str]:
    """Return budget ID matching provided ID or name."""
    if not identifier:
        return None
    for budget in budgets:
        if budget.get('id') == identifier or budget.get('name') == identifier:
            return budget.get('id')
    return None


def _build_account_mapping(df: pd.DataFrame, budget_id: str, accounts: List[dict]) -> Dict[str, str]:
    """Prompt for account mapping per unique `account_name`."""
    mapping: Dict[str, str] = {}
    if not accounts or 'account_name' not in df.columns:
        return mapping
    unique_names = sorted(
        {str(name).strip() for name in df['account_name'].dropna().unique() if str(name).strip()}
    )
    for name in unique_names:
        mapped_id = get_or_create_mapping(budget_id, name, accounts)
        if mapped_id:
            mapping[name.strip().lower()] = mapped_id
    return mapping


def _prompt_budget_for_owner(
        owner_label: str,
        budgets: List[dict],
        default_budget_id: Optional[str],
) -> Optional[str]:
    print(f"\nOwner '{owner_label}' has no budget mapping.")
    selected = _select_budget_interactive(budgets, default_budget_id)
    if not selected:
        logger.warning("No budget selected for owner %s. Transactions will be skipped.", owner_label)
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


def _resolve_budget_for_owner(
        owner_label: str,
        state: Optional[EmailStateStore],
        owner_budget_cache: Dict[str, str],
        budgets: List[dict],
        default_budget_id: Optional[str],
        force_budget_id: Optional[str],
) -> Optional[str]:
    key = _owner_cache_key(owner_label) or owner_label.lower()
    if force_budget_id:
        owner_budget_cache[key] = force_budget_id
        return force_budget_id
    if key in owner_budget_cache:
        return owner_budget_cache[key]
    stored = state.get_owner_budget(owner_label) if state else None
    if stored:
        owner_budget_cache[key] = stored
        return stored
    selected = _prompt_budget_for_owner(owner_label, budgets, default_budget_id)
    if selected and state:
        state.set_owner_budget(owner_label, selected)
    if selected:
        owner_budget_cache[key] = selected
    return selected


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
        logger.info("%s finished with no work to report.", title)
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
            f"- {label}: files={attachments}, parsed={parsed}, prepared={prepared}, uploaded={uploaded}, skipped={skipped}")
        warnings = data.get('warnings', [])
        if warnings:
            for warn in warnings:
                print(f"    ! {warn}")


def _handle_local_inputs(
        paths: Iterable[str],
        ynab_client: YNABClient,
        budgets: List[dict],
        default_budget_id: Optional[str],
        default_account_id: Optional[str],
        force_budget_id: Optional[str] = None,
) -> None:
    files = process_local_files(list(paths))
    if not files:
        logger.info("No local files found to process.")
        return

    state = EmailStateStore()
    owner_budget_cache: Dict[str, str] = {}
    budget_accounts_cache: Dict[str, List[dict]] = {}
    owner_batches: Dict[str, Dict[str, object]] = {}
    owner_summary: Dict[str, Dict[str, object]] = {}
    processed_paths: Set[str] = set()

    for identifier, content in files:
        display_name = identifier
        logger.info("Processing file: %s", display_name)
        df = process_transaction_file(display_name, content)
        if df is None or df.empty:
            logger.warning("No transactions parsed from %s", display_name)
            continue

        fallback_label = os.path.basename(display_name) if isinstance(display_name, str) else 'local-source'
        owner_groups = _group_df_by_owner(df, fallback_label)
        for owner_label, owner_df in owner_groups:
            owner_key = _owner_cache_key(owner_label) or owner_label
            entry = owner_summary.setdefault(owner_key, _init_owner_summary_entry(owner_label))
            entry['label'] = owner_label
            entry['sources'].add(display_name)
            entry['parsed'] += 1

            budget_id = _resolve_budget_for_owner(
                owner_label,
                state,
                owner_budget_cache,
                budgets,
                default_budget_id,
                force_budget_id,
            )
            if not budget_id:
                entry['warnings'].append(f"Missing budget mapping for {display_name}")
                entry['skipped'] += 1
                continue

            accounts = budget_accounts_cache.get(budget_id)
            if accounts is None:
                accounts = ynab_client.list_accounts(budget_id)
                budget_accounts_cache[budget_id] = accounts
                if not accounts:
                    logger.warning("No accounts retrieved for budget %s.", budget_id)
            if not accounts and not default_account_id:
                entry['warnings'].append(
                    f"No YNAB accounts available for budget {budget_id}. Set YNAB_ACCOUNT_ID or create an account."
                )
                entry['skipped'] += 1
                continue

            name_map = _build_account_mapping(owner_df, budget_id, accounts)
            if 'account_name' in owner_df.columns:
                missing_names = [
                    nm for nm in {str(n).strip() for n in owner_df['account_name'].dropna().unique()}
                    if nm.strip().lower() not in name_map and not default_account_id
                ]
                if missing_names:
                    entry['warnings'].append(f"Missing account mapping for {', '.join(missing_names)}")
                    entry['skipped'] += 1
                    continue

            transactions = convert_to_ynab_format(
                owner_df,
                default_account_id,
                name_map if name_map else None,
            )
            if not transactions:
                entry['warnings'].append(f"No transactions ready after formatting: {display_name}")
                entry['skipped'] += 1
                continue

            batch = owner_batches.setdefault(
                owner_key,
                {'label': owner_label, 'budget_id': budget_id, 'transactions': []},
            )
            batch['transactions'].extend(transactions)
            entry['prepared'] += len(transactions)

        if isinstance(display_name, str) and os.path.exists(display_name):
            processed_paths.add(display_name)

    state.save()

    if not owner_batches:
        logger.info("No transactions to upload from local files.")
        _print_owner_summary(owner_summary, "Local Import Summary")
        return

    all_success = True
    for owner_key, batch in owner_batches.items():
        transactions = batch['transactions']
        if not transactions:
            owner_summary[owner_key]['warnings'].append("No transactions prepared for upload.")
            continue
        budget_id = batch['budget_id']
        logger.info(
            "Uploading %d transactions for owner %s to budget %s.",
            len(transactions),
            batch['label'],
            budget_id,
        )
        success = ynab_client.upload_transactions(transactions, budget_id)
        if success:
            owner_summary[owner_key]['uploaded'] += len(transactions)
        else:
            owner_summary[owner_key]['warnings'].append("YNAB upload failed.")
            all_success = False

    if all_success:
        for path in processed_paths:
            try:
                os.rename(path, f"{path}.done")
                logger.info("Renamed %s to mark as processed.", path)
            except Exception as exc:
                logger.warning("Failed to rename %s: %s", path, exc)
        archive_last_month(list(paths))
    else:
        logger.warning("One or more uploads failed; source files left untouched for retry.")

    _print_owner_summary(owner_summary, "Local Import Summary")


def _process_email_flow(
        ynab_client: YNABClient,
        budgets: List[dict],
        config: dict,
        force_budget_id: Optional[str] = None,
) -> None:
    state = EmailStateStore()
    imap_server = config['email']['imap_server']
    email_address = config['email']['email']
    password = config['email']['password']
    auth_method = config['email'].get('auth_method', 'basic')
    oauth_config = config['email'].get('oauth')
    use_ssl = config['email'].get('use_ssl', True)
    starttls = config['email'].get('starttls', False)
    imap_id = config['email'].get('imap_id')
    senders = config['email'].get('senders', [])
    discover_enabled = config['email'].get('discover_senders', True)
    discover_sample = int(config['email'].get('discover_sample', 5) or 5)
    header_search_fallback = config['email'].get('header_search_fallback', True)
    fallback_scan_limit = int(config['email'].get('search_sample_limit', 10) or 10)

    mail = connect_email(
        imap_server,
        email_address,
        password,
        auth_method=auth_method,
        oauth_config=oauth_config,
        use_ssl=use_ssl,
        starttls=starttls,
        imap_id=imap_id,
    )
    if not mail:
        logger.error("Email ingestion aborted due to connection failure.")
        return

    mailbox_key = f"{imap_server}|{email_address}"
    processed_uids = state.get_processed_uids(mailbox_key)
    result = download_attachments(
        mail,
        senders,
        mailbox_key,
        processed_uids,
        header_search_fallback=header_search_fallback,
        fallback_scan_limit=fallback_scan_limit,
    )
    if not result.attachments and discover_enabled:
        logger.info("No attachments matched configured senders; attempting sender discovery.")
        _log_recent_senders(mail, discover_sample)
    try:
        mail.logout()
    except Exception:
        logger.debug("IMAP logout raised an exception.", exc_info=True)

    if not result.attachments:
        if result.warnings:
            logger.info("No new attachments. Warnings: %s", ", ".join(result.warnings))
        else:
            logger.info("No new email attachments discovered.")
        return

    default_budget_id = config['ynab'].get('budget_id')
    default_account_id = config['ynab'].get('account_id')
    owner_summary: Dict[str, Dict[str, object]] = {}
    owner_budget_cache: Dict[str, str] = {}
    budget_accounts_cache: Dict[str, List[dict]] = {}
    owner_batches: Dict[str, Dict[str, object]] = {}
    owner_message_map: Dict[str, Set[str]] = defaultdict(set)
    message_blocked: Set[str] = set()
    message_handled: Set[str] = set()

    for attachment in result.attachments:
        message_handled.add(attachment.message_uid)
        fallback_label = attachment.sender or attachment.filename
        df = process_transaction_file(attachment.filename, attachment.content)
        if df is None or df.empty:
            owner_key = _owner_cache_key(fallback_label) or fallback_label
            entry = owner_summary.setdefault(owner_key, _init_owner_summary_entry(fallback_label))
            entry['label'] = fallback_label
            entry['sources'].add(attachment.filename)
            entry['messages'].add(attachment.message_uid)
            entry['skipped'] += 1
            entry['warnings'].append(f"Parse failure: {attachment.filename}")
            message_blocked.add(attachment.message_uid)
            continue

        owner_groups = _group_df_by_owner(df, fallback_label)
        attachment_failed = False
        prepared_batches: List[Tuple[str, str, str, List[dict]]] = []

        for owner_label, owner_df in owner_groups:
            owner_key = _owner_cache_key(owner_label) or owner_label
            entry = owner_summary.setdefault(owner_key, _init_owner_summary_entry(owner_label))
            entry['label'] = owner_label
            entry['sources'].add(attachment.filename)
            entry['messages'].add(attachment.message_uid)
            entry['parsed'] += 1

            budget_id = _resolve_budget_for_owner(
                owner_label,
                state,
                owner_budget_cache,
                budgets,
                default_budget_id,
                force_budget_id,
            )
            if not budget_id:
                entry['warnings'].append(f"Missing budget mapping for {attachment.filename}")
                entry['skipped'] += 1
                attachment_failed = True
                break

            accounts = budget_accounts_cache.get(budget_id)
            if accounts is None:
                accounts = ynab_client.list_accounts(budget_id)
                budget_accounts_cache[budget_id] = accounts
                if not accounts:
                    logger.warning("No accounts retrieved for budget %s.", budget_id)
            if not accounts and not default_account_id:
                entry['warnings'].append(
                    f"No YNAB accounts available for budget {budget_id}. Set YNAB_ACCOUNT_ID or create an account."
                )
                entry['skipped'] += 1
                attachment_failed = True
                break

            name_map = _build_account_mapping(owner_df, budget_id, accounts)
            if 'account_name' in owner_df.columns:
                missing_names = [
                    nm for nm in {str(n).strip() for n in owner_df['account_name'].dropna().unique()}
                    if nm.strip().lower() not in name_map and not default_account_id
                ]
                if missing_names:
                    entry['warnings'].append(f"Missing account mapping for {', '.join(missing_names)}")
                    entry['skipped'] += 1
                    attachment_failed = True
                    break

            transactions = convert_to_ynab_format(
                owner_df,
                default_account_id,
                name_map if name_map else None,
            )
            if not transactions:
                entry['warnings'].append(f"No transactions ready after formatting: {attachment.filename}")
                entry['skipped'] += 1
                attachment_failed = True
                break

            prepared_batches.append((owner_key, owner_label, budget_id, transactions))

        if attachment_failed:
            message_blocked.add(attachment.message_uid)
            continue

        for owner_key, owner_label, budget_id, transactions in prepared_batches:
            batch = owner_batches.setdefault(
                owner_key,
                {'label': owner_label, 'budget_id': budget_id, 'transactions': []},
            )
            batch['transactions'].extend(transactions)
            owner_summary[owner_key]['prepared'] += len(transactions)
            owner_message_map[owner_key].add(attachment.message_uid)

    successful_uids: Set[str] = set()

    for owner_key, batch in owner_batches.items():
        transactions = batch['transactions']
        entry = owner_summary[owner_key]
        if not transactions:
            entry['warnings'].append("No transactions prepared for upload.")
            continue

        budget_id = batch['budget_id']
        logger.info(
            "Uploading %d transactions for owner %s to budget %s.",
            len(transactions),
            batch['label'],
            budget_id,
        )
        success = ynab_client.upload_transactions(transactions, budget_id)
        if success:
            entry['uploaded'] += len(transactions)
            for uid in owner_message_map[owner_key]:
                if uid not in message_blocked:
                    successful_uids.add(uid)
        else:
            entry['warnings'].append("YNAB upload failed.")
            for uid in owner_message_map[owner_key]:
                message_blocked.add(uid)

    # Messages that produced no transactions but were otherwise handled count as processed
    for uid in message_handled:
        if uid not in message_blocked:
            successful_uids.add(uid)

    if successful_uids:
        state.add_processed_uids(mailbox_key, successful_uids)
    state.save()
    _print_owner_summary(owner_summary, "Email Ingestion Summary")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
    )
    parser = argparse.ArgumentParser(description='Import transactions to YNAB from email or local files')
    parser.add_argument('--files', nargs='+', help='Process local files instead of email attachments')
    parser.add_argument(
        '--force-budget',
        help='Force all transactions (email or local) into the specified YNAB budget ID or name for this run.',
    )
    args = parser.parse_args()

    config = load_config()
    if not config['ynab'].get('api_key'):
        logger.error("YNAB_API_KEY is required.")
        return

    ynab_client = YNABClient(config['ynab']['api_key'])
    budgets = ynab_client.list_budgets()
    if not budgets:
        logger.error('Could not retrieve YNAB budgets. Check API key/connectivity.')
        return

    force_budget_id = None
    if args.force_budget:
        force_budget_id = _match_budget_identifier(budgets, args.force_budget)
        if not force_budget_id:
            logger.error("Force budget '%s' did not match any YNAB budget id or name.", args.force_budget)
            return

    if args.files:
        default_budget_id = force_budget_id or _select_budget_interactive(budgets, config['ynab'].get('budget_id'))
        if not default_budget_id and not force_budget_id:
            logger.error('No default budget selected. Aborting.')
            return
        default_account_id = config['ynab'].get('account_id')
        _handle_local_inputs(
            args.files,
            ynab_client,
            budgets,
            default_budget_id,
            default_account_id,
            force_budget_id,
        )
    else:
        _process_email_flow(ynab_client, budgets, config, force_budget_id=force_budget_id)


if __name__ == "__main__":
    main()
