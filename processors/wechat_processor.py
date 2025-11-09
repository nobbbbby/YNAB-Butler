import logging
from typing import Optional

import pandas as pd


def process_wechat(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Process WeChat Pay transaction data to a standard format.

    Handles cases where the header row appears within the sheet (common in WeChat exports),
    cleans amounts, formats dates, and aligns field names with the rest of the pipeline.
    """
    if df is None or getattr(df, 'empty', False):
        return df

    # Drop completely empty columns and rows
    # df = df.loc[:, ~df.columns.astype(str).str.contains('^Unnamed', na=False)]
    df = df.dropna(how='all')
    logging.debug(f"WeChat raw columns: {[str(c) for c in df.columns]}")

    # If standard header is not in columns, but present within the sheet, re-header
    header_tokens = ['交易时间', '日期']
    if not any(tok in df.columns.astype(str).tolist() for tok in header_tokens):
        header_idx = None
        search_limit = min(50, len(df))
        for i in range(search_limit):
            row_values = [str(v).strip() for v in df.iloc[i].tolist()]
            if any(tok in row_values for tok in header_tokens):
                header_idx = i
                break
        if header_idx is not None:
            new_cols = [str(v).strip() for v in df.iloc[header_idx].tolist()]
            df = df.iloc[header_idx + 1 :].copy()
            df.columns = new_cols
            logging.info(f"WeChat: detected embedded header at row {header_idx}")

    # Safely trim whitespace in column names first (before rename)
    df.columns = [str(c).strip() for c in df.columns]

    # Define mapping after potential re-header
    mapping = {
        '交易时间': 'date',
        '交易对方': 'payee_name',
        '商品': 'memo',
        '金额(元)': 'amount',
        '金额': 'amount',
        '收/支': 'transaction_type',
        '当前状态': 'status',
        '支付方式': 'account_name',
        '微信昵称': 'owner_name',
    }
    # Now rename known columns if present
    df = df.rename(columns={k: v for k, v in mapping.items() if k in df.columns})
    logging.info(f"WeChat columns after rename: {[str(c) for c in df.columns]}")

    # Trim object cells
    obj_cols = df.select_dtypes(include=['object']).columns

    for col in obj_cols:
            df[col] = df[col].apply(lambda v: v.strip() if isinstance(v, str) else v)

    # Fallback inference for critical columns if still missing
    if 'date' not in df.columns:
        candidates = [c for c in df.columns if ('时间' in str(c)) or (str(c) == '日期')]
        if candidates:
            df = df.rename(columns={candidates[0]: 'date'})
    if 'amount' not in df.columns:
        amt_candidates = [c for c in df.columns if '金额' in str(c)]
        if amt_candidates:
            df = df.rename(columns={amt_candidates[0]: 'amount'})

    # Clean and sign amounts
    if 'amount' in df.columns:
        # Remove commas and currency symbols if any
        df['amount'] = (
            df['amount']
            .astype(str)
            .str.replace(',', '', regex=False)
            .str.replace('¥', '', regex=False)
            .str.replace('￥', '', regex=False)
            .str.strip()
        )
        if 'transaction_type' in df.columns:
            df['amount'] = df.apply(
                lambda x: -abs(float(x['amount'])) if str(x.get('transaction_type', '')).strip() == '支出' else abs(float(x['amount'])),
                axis=1,
            )
        else:
            # Fallback: cast to float without changing sign
            df['amount'] = df['amount'].apply(lambda v: float(v) if v not in (None, '', 'nan') else 0.0)

    # Normalize date format
    if 'date' in df.columns:
        # Normalize empty-like strings to NaN and coerce
        df['date'] = df['date'].replace({'': None, 'nan': None, 'NaN': None})
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        before = len(df)
        # Drop rows where date failed to parse
        df = df[df['date'].notna()]
        dropped = before - len(df)
        if dropped:
            logging.info(f"WeChat: dropped {dropped} rows with unparsable dates")
        df['date'] = df['date'].dt.strftime('%Y-%m-%d')
    else:
        # No identifiable date column; avoid uploading bad rows
        logging.warning("WeChat: no date-like column identified; returning empty DataFrame")
        return df.iloc[0:0]

    if 'owner_name' not in df.columns:
        df['owner_name'] = None

    # Keep only relevant columns if they exist; otherwise keep as-is
    desired = ['date', 'amount', 'payee_name', 'memo', 'status', 'account_name', 'owner_name']
    existing = [c for c in desired if c in df.columns]
    if existing:
        df = df[existing + [c for c in df.columns if c not in existing]]

    # Drop rows missing critical fields (date and amount) if present
    present = [c for c in ['date', 'amount'] if c in df.columns]
    if present:
        # Treat empty strings as NaN for object cols prior to drop
        for c in present:
            if c in df.columns and df[c].dtype == object:
                df[c] = df[c].replace({'': None, 'nan': None, 'NaN': None})
        df = df.dropna(subset=present)

    return df
