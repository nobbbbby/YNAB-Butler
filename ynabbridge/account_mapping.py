from typing import Dict, List, Optional

from importers.email_importer import EmailStateStore

_STATE_STORE: Optional[EmailStateStore] = None


def _store() -> EmailStateStore:
    global _STATE_STORE
    if _STATE_STORE is None:
        _STATE_STORE = EmailStateStore()
    return _STATE_STORE


def load_mappings(_: str = '') -> Dict[str, str]:
    """Return persisted account/transaction name -> YNAB account_id mappings."""
    return _store().get_all_account_mappings()


def save_mappings(mappings: Dict[str, str], _: str = '') -> None:
    store = _store()
    store.replace_account_mappings(mappings)
    store.save()


def select_account_interactive(accounts: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not accounts:
        print('No YNAB accounts found for this budget.')
        return None
    print('\nAvailable YNAB Accounts:')
    for idx, acc in enumerate(accounts, start=1):
        print(f"  {idx}. {acc.get('name')}  [{acc.get('type')}]  ({acc.get('id')})")
    while True:
        choice = input('Select account by number: ').strip()
        if not choice.isdigit():
            print('Please enter a valid number.')
            continue
        i = int(choice)
        if 1 <= i <= len(accounts):
            return accounts[i - 1]
        print('Out of range, try again.')


def get_or_create_mapping(
        budget_id: str,
        account_name: str,
        accounts: List[Dict[str, str]],
        store: Optional[EmailStateStore] = None,
) -> Optional[str]:
    """Return mapped YNAB account_id for a transaction/account name.
    If no mapping exists, list all YNAB accounts by name and prompt the user to select one,
    then persist the mapping for future runs.
    """
    store = store or _store()
    existing = store.get_account_mapping(budget_id, account_name)
    if existing:
        return existing
    # Ask user to select from all YNAB accounts; do not auto-match by name as they may differ
    print(f"No existing mapping for account/transaction name '{account_name}'.")
    selected = select_account_interactive(accounts)
    if not selected:
        return None
    store.set_account_mapping(budget_id, account_name, selected['id'])
    store.save()
    return selected['id']
