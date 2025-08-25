import pandas as pd
from typing import List, Dict, Optional


def convert_to_ynab_format(
    df: pd.DataFrame,
    account_id: Optional[str] = None,
    name_to_account_id: Optional[Dict[str, str]] = None,
) -> List[Dict]:
    """Convert standardized DataFrame to YNAB transaction format.

    Expects a DataFrame with columns like 'date', 'amount', 'payee' or 'payee_name', 'memo', and optional 'status'.
    You can pass either a single account_id for all transactions, or a per-name mapping via name_to_account_id
    where keys are origin/transaction names (e.g., payee_name) and values are YNAB account IDs.

    If both are provided and a name is not in the mapping, the single account_id is used as fallback.
    """
    transactions: List[Dict] = []
    for _, row in df.iterrows():
        payee = row.get('payee_name', row.get('payee', ''))
        origin = row.get('account_name', None)
        amount_val = row.get('amount', 0)
        try:
            amount_milli = amount_val if isinstance(amount_val, int) else int(float(amount_val) * 1000)
        except Exception:
            amount_milli = 0
        # Determine account_id per transaction
        acct = None
        # Prefer mapping by origin_name if present, else by payee_name/payee
        key_candidates: List[str] = []
        if isinstance(origin, str) and origin.strip():
            key_candidates.append(origin.strip().lower())
        if name_to_account_id:
            for k in key_candidates:
                if k in name_to_account_id:
                    acct = name_to_account_id[k]
                    break
        if not acct:
            acct = account_id
        transaction = {
            'account_id': acct,
            'date': row.get('date', ''),
            'amount': amount_milli,
            'payee_name': payee,
            'memo': row.get('memo', ''),
            'import_id': f"YNAB:{row.get('date', '')}:{row.get('amount', 0)}",
            'cleared': 'cleared' if row.get('status', '') in ('交易成功', 'cleared') else 'uncleared'
        }
        transactions.append(transaction)
    return transactions
