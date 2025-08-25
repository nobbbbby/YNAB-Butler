import argparse
import logging
import pandas as pd
from config import load_config
from importers.email_importer import connect_email, download_attachments
from importers.local_importer import process_local_files, archive_last_month
from processors.alipay_processor import process_alipay
from processors.wechat_processor import process_wechat
from ynabbridge.ynab_client import YNABClient
import io
from ynabbridge.formatter import convert_to_ynab_format
from ynabbridge.account_mapping import get_or_create_mapping

def detect_platform(filename: str) -> str:
    """Detect payment platform from filename."""
    fname = filename.lower()
    if 'alipay' in fname or '支付宝' in fname:
        return 'alipay'
    elif 'wechat' in fname or '微信' in fname:
        return 'wechat'
    return 'unknown'


def process_transaction_file(filename: str, content: bytes) -> pd.DataFrame:
    """Process a single transaction file and return standardized DataFrame, or None."""
    platform = detect_platform(filename)
    logging.info(f"Detected platform '{platform}' for file: {filename}")
    if platform == 'alipay':
        logging.info(f"Using Alipay parser for file: {filename}")
        from processors.alipay_processor import parse_alipay_csv, process_alipay
        df = parse_alipay_csv(content)
        if df is None or getattr(df, 'empty', False):
            logging.error(f"parse_alipay_csv returned empty/None for file: {filename}")
            return None
        logging.debug(f"Alipay CSV parsed: shape={df.shape}, columns={[c for c in df.columns]}")
        processed_df = process_alipay(df)
        if processed_df is None or getattr(processed_df, 'empty', False):
            logging.warning(f"process_alipay produced empty/None for file: {filename}")
            return None
        logging.debug(f"Alipay processed: shape={processed_df.shape}, columns={[c for c in processed_df.columns]}")
        return processed_df
    elif platform == 'wechat':
        logging.info(f"Using WeChat parser for file: {filename}")
        from processors.wechat_processor import process_wechat
        # Decide parser based on extension: WeChat exports are often .csv or .xlsx
        lower_name = filename.lower()
        if lower_name.endswith('.xlsx') or lower_name.endswith('.xls'):
            try:
                df = pd.read_excel(io.BytesIO(content))
                logging.debug(f"WeChat Excel read: shape={df.shape}")
            except Exception as e:
                logging.error(f"Failed to read WeChat Excel: {filename}, error: {str(e)}")
                return None
        else:
            encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030']
            for encoding in encodings:
                try:
                    content_str = content.decode(encoding)
                    df = pd.read_csv(io.StringIO(content_str))
                    logging.debug(f"WeChat CSV read with encoding={encoding}, shape={df.shape}")
                    break
                except UnicodeDecodeError:
                    continue
            else:
                logging.error(f"Failed to decode CSV: {filename}")
                return None
        return process_wechat(df)
    # Fallback for generic CSV/Excel, but block Alipay files by header check
    if filename.lower().endswith('.csv'):
        encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030']
        for encoding in encodings:
            try:
                content_str = content.decode(encoding)
                # Defensive: check for Alipay CSV header and block generic parsing
                if '支付宝交易记录明细' in content_str or '支付宝' in content_str[:100]:
                    logging.error(f"Detected Alipay CSV structure in generic CSV logic for file: {filename}. Aborting parse.")
                    return None
                df = pd.read_csv(io.StringIO(content_str))
                logging.debug(f"Generic CSV read with encoding={encoding}, shape={df.shape}")
                break
            except UnicodeDecodeError:
                continue
        else:
            logging.error(f"Failed to decode CSV: {filename}")
            return None
    else:
        try:
            df = pd.read_excel(io.BytesIO(content))
            logging.debug(f"Excel read: shape={df.shape}")
        except Exception as e:
            logging.error(f"Failed to read Excel: {filename}, error: {str(e)}")
            return None
    df.columns = df.columns.str.strip().str.lower()
    return df

def _select_budget_interactive(budgets, current_id: str | None) -> str | None:
    """Prompt the user to select a budget from YNAB budgets.
    Returns the selected budget id, or None if cancelled.
    """
    if not budgets:
        return None
    # Find default index if current_id is provided
    default_idx = None
    if current_id:
        for i, b in enumerate(budgets):
            if b.get('id') == current_id:
                default_idx = i
                break
    print("Select YNAB budget:")
    for i, b in enumerate(budgets):
        marker = "*" if default_idx is not None and i == default_idx else " "
        print(f"  [{i}] {marker} {b.get('name')} ({b.get('id')})")
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

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
    parser = argparse.ArgumentParser(description='Import transactions to YNAB from email or local files')
    parser.add_argument('--files', nargs='+', help='Process local files instead of email attachments')
    args = parser.parse_args()
    config = load_config()
    ynab_client = YNABClient(config['ynab']['api_key'])
    # Always fetch budgets and prompt user to pick one (default to configured one if present)
    budgets = ynab_client.list_budgets()
    if not budgets:
        logging.error('Could not retrieve YNAB budgets. Check API key/connectivity.')
        return
    budget_id = _select_budget_interactive(budgets, config['ynab'].get('budget_id'))
    if not budget_id:
        logging.error('No budget selected. Aborting.')
        return

    # Fetch accounts for mapping
    accounts = ynab_client.list_accounts(budget_id) if budget_id else []
    if not accounts:
        logging.warning('Could not fetch YNAB accounts. Will fall back to configured YNAB_ACCOUNT_ID if provided.')

    # Default fallback account if some names remain unmapped
    default_account_id = config['ynab'].get('account_id')
    if not default_account_id and not accounts:
        logging.error('No YNAB accounts available and YNAB_ACCOUNT_ID not set. Cannot continue.')
        return

    all_transactions = []
    processed_source_files = []  # files on disk that produced transactions, to rename upon successful upload
    if args.files:
        files = process_local_files(args.files)
    else:
        mail = connect_email(config['email']['imap_server'], config['email']['email'], config['email']['password'])
        if not mail:
            logging.error('Could not connect to email.')
            return
        files = download_attachments(mail, config['email']['senders'])
    for identifier, content in files:
        # identifier is full path for local files, or inner filename for archives/email
        log_name = identifier
        logging.info(f"Processing file: {log_name}")
        df = process_transaction_file(log_name if isinstance(log_name, str) else str(log_name), content)
        if df is not None and not df.empty:
            # Build name->account_id mapping for all unique origin/transaction names in this file
            name_map = {}
            if accounts:
                # Prefer origin_name column (e.g., 收/付款方式 from Alipay). Fallback to payee_name/payee if absent
                name_series = None
                if 'account_name' in df.columns:
                    name_series = df['account_name']
                if name_series is not None:
                    unique_names = sorted({str(x).strip() for x in name_series.dropna().unique() if str(x).strip()})
                    for nm in unique_names:
                        mapped_id = get_or_create_mapping(budget_id, nm, accounts)
                        if mapped_id:
                            name_map[nm.strip().lower()] = mapped_id
            ynab_transactions = convert_to_ynab_format(df, default_account_id, name_map if name_map else None)
            if ynab_transactions:
                all_transactions.extend(ynab_transactions)
                # Track local file path for potential rename
                try:
                    import os
                    if os.path.exists(log_name):
                        processed_source_files.append(log_name)
                except Exception:
                    pass
    if all_transactions:
        success = ynab_client.upload_transactions(all_transactions, budget_id)
        if success:
            logging.info('Transaction import completed successfully')
            # Rename processed source files with .done suffix
            import os
            for src in sorted(set(processed_source_files)):
                try:
                    if not src.lower().endswith('.done'):
                        new_name = src + '.done'
                        if os.path.exists(new_name):
                            # Avoid collision: append a counter
                            base = new_name
                            i = 1
                            while os.path.exists(f"{base}.{i}"):
                                i += 1
                            new_name = f"{base}.{i}"
                        os.rename(src, new_name)
                        logging.info(f"Renamed processed file to: {new_name}")
                except Exception as e:
                    logging.warning(f"Failed to rename {src} to .done: {e}")
            # Archive last month's files in provided directories (if any)
            if args.files:
                archive_last_month(args.files)
        else:
            logging.warning('Transaction import completed with issues')
    else:
        logging.warning('No transactions to import')

if __name__ == "__main__":
    main()
