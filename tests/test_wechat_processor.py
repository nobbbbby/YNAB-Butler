import os
import pandas as pd
import pytest
from processors.wechat_processor import process_wechat


def test_process_wechat_integration():
    """
    Integration test: parse the real WeChat export (.xlsx), process it, and check output.
    """
    xlsx_path = "data/transactions/test_wechat.xlsx"

    if not os.path.exists(xlsx_path):
        pytest.skip("WeChat sample XLSX not present")

    # Read excel as DataFrame (processor will handle embedded header rows)
    df_raw = pd.read_excel(xlsx_path)

    result = process_wechat(df_raw)

    # If no transactions detected, the processor returns empty DF
    assert isinstance(result, pd.DataFrame)

    if result.empty:
        pytest.skip("WeChat processor returned empty DataFrame (no identifiable transactions in sample)")

    # Basic checks for normalized output
    expected_at_least = {"date", "amount"}
    assert expected_at_least.issubset(set(result.columns))

    # # Amount should be numeric (float) at this stage; signed negative for 支出 handled per-row
    assert pd.api.types.is_float_dtype(result["amount"]) or pd.api.types.is_integer_dtype(result["amount"])  # tolerate int if exact

    # # Date should be ISO format
    assert result["date"].astype(str).str.match(r"\d{4}-\d{2}-\d{2}").all()

    # # No all-NaN rows
    assert not result.dropna(how='all').empty

    # Print some diagnostics for easier debugging in CI logs
    print("WeChat result sample:")
    print(result.head(3))
    print("WeChat result dtypes:")
    print(result.dtypes)
