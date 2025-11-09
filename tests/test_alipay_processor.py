import os

import pandas as pd
import pytest

from processors.alipay_processor import process_alipay, parse_alipay_csv


def test_process_alipay_integration():
    """
    Integration test: parse the real Alipay export, process it, and check output.
    """
    csv_path = "data/transactions/test_alipay.csv"

    if not os.path.exists(csv_path):
        pytest.skip("Alipay sample CSV not present")
    with open(csv_path, "rb") as f:
        content = f.read()
    df = parse_alipay_csv(content)
    result = process_alipay(df)
    # Basic checks for new YNAB-compatible output
    expected_cols = {'status', 'date', 'amount', 'payee_name', 'memo', 'owner_name'}
    assert expected_cols.issubset(result.columns)
    # Amount should be int (milliunits)
    assert pd.api.types.is_integer_dtype(result['amount'])
    # Date should be ISO format
    assert result['date'].str.match(r'\d{4}-\d{2}-\d{2}').all()
    # No all-NaN rows
    assert not result.dropna(how='all').empty
    print(df.describe())
    print("Sample data of result:")
    print(result.head(3))
    print("Schema of result:")
    print(result.dtypes)
    # result.to_csv('data/alipay_example.csv', index=False)
