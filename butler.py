from __future__ import annotations

import argparse
import io
import logging
import os
from typing import Dict, Iterable, List, Optional, Set

import pandas as pd

from config import load_config
from importers.email_importer import (
    EmailStateStore,
    _log_recent_senders,
    connect_email,
    download_attachments,
)
from importers.ingestion_engine import (
    IngestionEngine,
    IngestionItem,
    SourceCallbacks,
    select_budget_interactive,
)
from importers.local_importer import archive_last_month, process_local_files
from processors.alipay_processor import parse_alipay_csv, process_alipay
from processors.wechat_processor import process_wechat
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
    logger.debug("Detected platform '%s' for file: %s", platform, filename)
    if platform == 'alipay':
        logger.debug("Using Alipay parser for file: %s", filename)
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
        logger.debug("Using WeChat parser for file: %s", filename)
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


def _match_budget_identifier(budgets: List[dict], identifier: str) -> Optional[str]:
    """Return budget ID matching provided ID or name."""
    if not identifier:
        return None
    for budget in budgets:
        if budget.get('id') == identifier or budget.get('name') == identifier:
            return budget.get('id')
    return None


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
    engine = IngestionEngine(
        ynab_client,
        budgets,
        state,
        default_budget_id,
        default_account_id,
        force_budget_id,
    )
    items: List[IngestionItem] = []
    processed_paths: Set[str] = set()
    for idx, (identifier, content) in enumerate(files):
        display_name = str(identifier)
        logger.debug("Processing file: %s", display_name)
        df = process_transaction_file(display_name, content)
        fallback_label = os.path.basename(display_name) if isinstance(identifier, str) else 'local-source'
        if df is None or df.empty:
            logger.debug("No transactions parsed from %s", display_name)
            engine.record_source_warning(
                fallback_label,
                display_name,
                None,
                f"Parse failure: {display_name}",
            )
            continue
        metadata: Dict[str, object] = {}
        if isinstance(identifier, str) and os.path.exists(identifier):
            metadata['file_path'] = identifier
            processed_paths.add(identifier)
        items.append(
            IngestionItem(
                item_id=f"local::{idx}::{display_name}",
                display_name=display_name,
                dataframe=df,
                fallback_owner=fallback_label,
                source='local',
                metadata=metadata,
            )
        )

    engine.add_items(items)
    if not engine.has_items:
        logger.info("No transactions to upload from local files.")
        state.save()
        engine.print_summary("Local Import Summary")
        return

    result = engine.flush()
    if result.all_succeeded:
        for path in processed_paths:
            try:
                os.rename(path, f"{path}.done")
                logger.info("Renamed %s to mark as processed.", path)
            except Exception as exc:
                logger.warning("Failed to rename %s: %s", path, exc)
        archive_last_month(list(paths))
    else:
        logger.warning("One or more uploads failed; source files left untouched for retry.")

    state.save()
    engine.print_summary("Local Import Summary")


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
    engine = IngestionEngine(
        ynab_client,
        budgets,
        state,
        default_budget_id,
        default_account_id,
        force_budget_id,
    )
    processed_uids: Set[str] = set()

    def _on_success(item: IngestionItem) -> None:
        uids = item.metadata.get('message_uids', set()) or set()
        processed_uids.update(uids)

    callbacks = SourceCallbacks(on_success=_on_success)
    items: List[IngestionItem] = []
    for idx, attachment in enumerate(result.attachments):
        fallback_label = attachment.sender or attachment.filename
        df = process_transaction_file(attachment.filename, attachment.content)
        metadata = {'message_uids': {attachment.message_uid}}
        if df is None or df.empty:
            logger.debug("Parse failure for %s", attachment.filename)
            engine.record_source_warning(
                fallback_label,
                attachment.filename,
                metadata,
                f"Parse failure: {attachment.filename}",
            )
            continue
        items.append(
            IngestionItem(
                item_id=f"email::{idx}::{attachment.message_uid}",
                display_name=attachment.filename,
                dataframe=df,
                fallback_owner=fallback_label,
                source='email',
                metadata=metadata,
            )
        )

    engine.add_items(items, callbacks=callbacks)
    if engine.has_items:
        engine.flush()
        if processed_uids:
            state.add_processed_uids(mailbox_key, processed_uids)
    else:
        logger.info("No transactions ready from email attachments.")

    state.save()
    engine.print_summary("Email Ingestion Summary")


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
        default_budget_id = force_budget_id or select_budget_interactive(budgets, config['ynab'].get('budget_id'))
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
