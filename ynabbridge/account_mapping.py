import json
import os
from typing import Dict, List, Optional

MAPPINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'account_mappings.json')


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def load_mappings(path: str = MAPPINGS_FILE) -> Dict[str, str]:
    """Load persisted account/transaction name -> YNAB account_id mappings.
    Key format: "{budget_id}:{account_name_lower}" -> account_id
    """
    try:
        if not os.path.exists(path):
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_mappings(mappings: Dict[str, str], path: str = MAPPINGS_FILE) -> None:
    _ensure_dir(path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(mappings, f, ensure_ascii=False, indent=2)


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


def get_or_create_mapping(budget_id: str, account_name: str, accounts: List[Dict[str, str]]) -> Optional[str]:
    """Return mapped YNAB account_id for a transaction/account name.
    If no mapping exists, list all YNAB accounts by name and prompt the user to select one,
    then persist the mapping for future runs.
    """
    key = f"{budget_id}:{account_name.strip().lower()}"
    mappings = load_mappings()
    if key in mappings:
        return mappings[key]
    # Ask user to select from all YNAB accounts; do not auto-match by name as they may differ
    print(f"No existing mapping for account/transaction name '{account_name}'.")
    selected = select_account_interactive(accounts)
    if not selected:
        return None
    mappings[key] = selected['id']
    save_mappings(mappings)
    return selected['id']
