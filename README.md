# YNAB Butler: YNAB Transaction Importer (Alipay / WeChat)
[简体中文 README](./README.zh-CN.md)

Import personal transactions from Alipay and WeChat into YNAB using local files, folders, or secure email ingestion. The
CLI fetches IMAP attachments (including encrypted ZIP archives), normalizes them, and uploads transactions directly to
the mapped YNAB budgets after a one-time account mapping prompt.

After a successful upload, processed local files are renamed with a `.done` suffix, and last month’s files are archived per directory into `YYYY-MM.archive.zip`.

## Features

- Import sources:
  - Local files and entire directories (recursively)
  - Email import (IMAP) with secure passphrase prompts and per-sender routing
- Formats:
  - CSV and Excel (`.xlsx`/`.xls`)
  - Archives: `.zip` natively; `.rar`/`.7z` via optional tools
  - Robust CSV decoding attempts for common Chinese encodings (`utf-8`, `gbk`, `gb2312`, `gb18030`)
- Platform detection and parsing:
  - Alipay exports
  - WeChat exports
  - Generic CSV/Excel fallback (with safeguards to avoid mis-parsing Alipay CSVs)
- YNAB integration:
  - Interactive budget selection from your YNAB account
  - Automatic or user-assisted mapping of source account names to YNAB account IDs
  - Uploads using the YNAB API
- Post-processing:
  - Rename processed local files with `.done`
  - Monthly archival per provided directory (e.g., `2025-07.archive.zip`), based on modification dates
- Safety filters for local scanning:
  - Skips files ending with `.done`, `.archive`, `.archive.zip`, and those starting with `test_`

## Requirements

- Python 3.12+
- A YNAB Personal Access Token
- IMAP access to the mailbox that receives bank statements
- For Excel support: `openpyxl` (installed alongside `pandas`)
- For email ingestion: `imapclient` and `pyzipper` (installed with the project dependencies)
- For `.rar`/`.7z` archives (optional):
  - Python package: `patool`
  - System tools: `7z` (p7zip) and/or `unrar`

## Setup

1. Create and activate a virtual environment (recommended):

   - macOS/Linux:
     ```
     python3 -m venv .venv
     source .venv/bin/activate
     ```
   - Windows (PowerShell):
     ```
     py -3 -m venv .venv
     .\.venv\Scripts\Activate.ps1
     ```

2. Install dependencies directly from the project (uses `pyproject.toml`):

    - Using `uv` (preferred so the resolved versions in `uv.lock` are honored):
      ```
      uv sync
      ```
      or, if you only want editable installation without running scripts:
     ```
     uv pip install -e .
     ```
    - Using `pip`:
     ```
     pip install -e .
     ```

   Optional (only if you need to open `.rar` / `.7z` archives):
   ```
   pip install patool
   ```
   And ensure the system tools are available on PATH:
    - macOS: `brew install p7zip unrar` (or equivalents)
    - Ubuntu/Debian: `sudo apt-get install p7zip-full unrar`
    - Windows: install 7-Zip and ensure it’s on PATH

## Configuration

The app expects a configuration object with the following structure (via `.env`):

- YNAB (`YNAB_*`):
    - `YNAB_API_KEY` (required): Personal Access Token
    - `YNAB_BUDGET_ID` (optional): Default budget to preselect when prompted
    - `YNAB_ACCOUNT_ID` (optional): Fallback YNAB account if no mapping exists
- Email (`EMAIL_*`):
    - `EMAIL_ADDRESS` (required for email mode)
    - `EMAIL_PASSWORD` (required for `EMAIL_AUTH_METHOD=basic`; app passwords recommended)
    - `IMAP_SERVER` (optional, default `imap.gmail.com`)
    - `EMAIL_IMAP_SSL` / `EMAIL_IMAP_STARTTLS` (optional booleans; default SSL on, STARTTLS off)
    - `EMAIL_IMAP_ID_NAME`, `EMAIL_IMAP_ID_VERSION`, `EMAIL_IMAP_ID_VENDOR`, `EMAIL_IMAP_ID_SUPPORT_EMAIL` (optional;
      IMAP ID metadata for providers that require it)
    - `EMAIL_IMAP_ID_EXTRA` (optional comma-separated `key=value` pairs to merge into the IMAP ID payload)
    - `EMAIL_SEARCH_HEADER_FALLBACK` (optional boolean; if true, retries `HEADER FROM` searches when server-side `FROM`
      filters return zero)
    - `EMAIL_SEARCH_SAMPLE_LIMIT` (optional integer, default 10; number of recent messages to scan when both standard
      searches return empty)
    - `EMAIL_DISCOVER_SENDERS` (optional boolean; when true, logs recent From addresses if no configured sender matches)
    - `EMAIL_DISCOVER_SAMPLE_LIMIT` (optional integer, default 5; number of recent messages to inspect for discovery
      logging)
    - `EMAIL_SENDERS` (comma-separated list of allowed sender addresses)
    - `EMAIL_PASSPHRASE` or `EMAIL_PASSPHRASE_<SENDER>` (optional; use only if you prefer non-interactive archive
      decryption)
    - `EMAIL_AUTH_METHOD` (optional; `basic` or `oauth`, default `basic`)
    - OAuth-specific (required when `EMAIL_AUTH_METHOD=oauth`):
        - `EMAIL_OAUTH_CLIENT_ID`, `EMAIL_OAUTH_TENANT_ID`, `EMAIL_OAUTH_CLIENT_SECRET`
        - `EMAIL_OAUTH_REFRESH_TOKEN` (user-delegated refresh token obtained via Azure consent)
        - `EMAIL_OAUTH_SCOPES` (optional; defaults to `https://outlook.office365.com/.default`)

Example configuration structure (the exact file format depends on your `config` module; ensure `load_config()` returns a dict with these keys):

## Usage

### Local files

```bash
python main.py --files /path/to/wechat_or_alipay_transactions.csv
```

### Email ingestion

Run without `--files` to poll email:

```bash
python main.py
```

The CLI will:

1. Connect to the configured IMAP mailbox and fetch attachments from approved senders.
2. Prompt once per new sender to map it to a YNAB budget.
3. Prompt for encrypted ZIP passphrases (or reuse in-memory for the run).
4. Upload transactions grouped by sender and display a per-sender summary.

Security notes:

- Passphrases are never brute-forced and remain in memory only for the current run (unless supplied via environment
  variables, which logs a warning).
- Decrypted attachments are streamed in-memory and not retained on disk after upload.
- Processed message UIDs are recorded under `.ynab-butler/state.json` to prevent duplicate ingestion; delete the file if
  you need to replay historical emails.
- Outlook / Office365 requires OAuth2 (“Modern Auth”). Set `EMAIL_AUTH_METHOD=oauth` plus the OAuth variables above; the
  CLI will refresh a short-lived access token securely before logging in via XOAUTH2.
- WeChat banks that deliver Tenpay download links instead of attachments are supported: the CLI fetches
  `https://tenpay.wechatpay.cn/userroll/userbilldownload/downloadfilefromemail...` URLs securely and treats them like
  attachments (no link is fetched outside that allowlist).
