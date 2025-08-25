# YNAB Butler: YNAB Transaction Importer (Alipay / WeChat)
[简体中文 README](./README.zh-CN.md)

Import personal transactions from Alipay and WeChat into YNAB using local files and folders (CSV/Excel, including archives).
Email importing over IMAP is planned but currently unfinished/experimental and not recommended for use yet.

After a successful upload, processed local files are renamed with a `.done` suffix, and last month’s files are archived per directory into `YYYY-MM.archive.zip`.

## Features

- Import sources:
  - Local files and entire directories (recursively)
  - Email import (IMAP) — planned/experimental and not finished
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

- Python 3.10+
- A YNAB Personal Access Token
- For Excel support: `openpyxl` (installed alongside `pandas`)
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

2. Install dependencies:

   - If a `requirements.txt` is present:
     ```
     pip install -r requirements.txt
     ```
   - Otherwise, install the core packages:
     ```
     pip install pandas ynab openpyxl
     ```
   - Optional (only if you need to open `.rar` / `.7z` archives):
     ```
     pip install patool
     ```
     And ensure the system tools are available on PATH:
     - macOS: `brew install p7zip unrar` (or equivalents)
     - Ubuntu/Debian: `sudo apt-get install p7zip-full unrar`
     - Windows: install 7-Zip and ensure it’s on PATH

## Configuration

The app expects a configuration object with the following structure:

- YNAB settings(`.env`):
  - `api_key` (required): Your YNAB Personal Access Token
  - `budget_id` (optional): Default budget to preselect
  - `account_id` (optional): Fallback YNAB account ID if mapping isn’t available

Email settings for IMAP importing are planned for a future version and are not required at this time.

Example configuration structure (the exact file format depends on your `config` module; ensure `load_config()` returns a dict with these keys):

## Usage
```python
python main.py --files /path/to/wechat_or_alipay_trandactions.csv
```