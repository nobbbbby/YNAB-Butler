import io
from typing import Optional, Union

import pandas as pd


def parse_alipay_csv(source: Union[str, bytes]) -> pd.DataFrame:
    """
    Parse an Alipay export CSV to extract the transaction table as a DataFrame.
    Skips metadata and finds the real header row.
    """
    encodings = ['utf-8', 'gbk', 'gb18030']
    if isinstance(source, str):
        # Source is file path
        with open(source, encoding='utf-8') as f:
            lines = f.readlines()
    elif isinstance(source, bytes):
        # Source is file content
        for enc in encodings:
            try:
                decoded = source.decode(enc)
                lines = io.StringIO(decoded).readlines()
                decoded_encoding = enc
                break
            except UnicodeDecodeError:
                continue
        else:
            raise UnicodeDecodeError(f"Could not decode content with tried encodings: {encodings}")
    else:
        raise ValueError("Invalid source type. It should be either a file path or file content.")
    # Find header row
    for idx, line in enumerate(lines):
        if line.strip().startswith("交易时间"):
            header_idx = idx
            break
    else:
        raise ValueError("No header row found in the CSV file.")
    if isinstance(source, str):
        df = pd.read_csv(source, skiprows=header_idx, encoding='utf-8')
    elif isinstance(source, bytes):
        df = pd.read_csv(io.StringIO(decoded), skiprows=header_idx)
    # Remove empty columns
    df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
    # Drop all-empty rows, if any
    df = df.dropna(how='all')
    return df


def process_alipay(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Process Alipay transaction data to a format compatible with YNAB API."""
    mapping = {
        '交易时间': 'date',
        '交易创建时间': 'date',
        '交易对方': 'payee_name',
        '商品说明': 'memo',  # Correct for your CSV
        '商品名称': 'memo',  # Optional: support both
        '金额': 'amount',
        '收/支': 'transaction_type',
        '交易状态': 'status',
        '收/付款方式': 'account_name',  # origin/transaction name used for account mapping
    }
    # Rename columns
    df = df.rename(columns={k: v for k, v in mapping.items() if k in df.columns})
    # Convert amount to negative for expenses, then to milliunits (int)
    if 'transaction_type' in df.columns and 'amount' in df.columns:
        df['amount'] = df.apply(
            lambda x: int(-abs(float(x['amount'])) * 1000) if x['transaction_type'] == '支出' else int(
                abs(float(x['amount'])) * 1000),
            axis=1
        )
    # Ensure date is in ISO format (YYYY-MM-DD)
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    # Add account_id column
    # Select only relevant columns for YNAB
    # Keep account_name in DF so main can build per-origin mapping prior to conversion
    columns = ['status', 'date', 'amount', 'payee_name', 'memo', 'account_name']
    df = df[[col for col in columns if col in df.columns]]
    # Drop rows with missing required fields
    df = df.dropna(subset=['date', 'amount', 'account_name'])
    return df
