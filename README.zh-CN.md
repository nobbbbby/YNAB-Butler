# YNAB Butler: YNAB 交易导入工具（支付宝 / 微信）

从本地文件、文件夹或安全的 IMAP 邮件附件（包含加密 ZIP）将个人交易导入 YNAB。CLI 会在首次运行时提示完成账户映射，并在之后自动下载、解密并上传交易。

上传成功后，已处理的本地文件会追加重命名为 `.done`，并且上一个自然月的文件会按目录打包归档为 `YYYY-MM.archive.zip`。

## 功能特性

- 导入来源：
  - 本地文件与整个目录（递归扫描）
  - 邮件导入（IMAP），支持安全口令提示及按发件人路由
- 支持格式：
  - CSV 与 Excel（`.xlsx`/`.xls`）
  - 压缩包：原生支持 `.zip`；可选工具支持 `.rar`/`.7z`
  - 针对常见中文编码进行鲁棒解码尝试（`utf-8`、`gbk`、`gb2312`、`gb18030`）
- 平台识别与解析：
  - 支付宝导出
  - 微信导出
  - 通用 CSV/Excel 兜底解析（包含避免将支付宝 CSV 误判的保护）
- 与 YNAB 集成：
  - 交互式选择目标预算
  - 根据来源账户/名称自动或半自动映射到 YNAB 账户 ID
  - 通过 YNAB API 执行上传
- 后处理：
  - 成功上传后为本地源文件追加 `.done` 后缀
  - 按提供的顶层目录将上一个自然月的文件打包为 `YYYY-MM.archive.zip`
- 本地扫描安全过滤：
  - 跳过以 `.done`、`.archive`、`.archive.zip` 结尾或以 `test_` 开头的文件

## 环境要求

- Python 3.10+
- YNAB 个人访问令牌（Personal Access Token）
- 可访问银行邮件收件箱的 IMAP 账号
- 读取 Excel 的支持：`openpyxl`（与 `pandas` 一起安装）
- 邮件导入依赖：`imapclient`、`pyzipper`（已包含在 `requirements.txt` 中）
- 可选的 `.rar`/`.7z` 解压支持：
  - Python 包：`patool`
  - 系统工具：`7z`（p7zip）和/或 `unrar`

## 环境搭建

1. 创建并激活虚拟环境（推荐）：

   - macOS/Linux:
     ```
     python3 -m venv .venv
     source .venv/bin/activate
     ```
   - Windows（PowerShell）:
     ```
     py -3 -m venv .venv
     .\.venv\Scripts\Activate.ps1
     ```

2. 安装依赖：

   - 若存在 `requirements.txt`：
     ```
     pip install -r requirements.txt
     ```
   - 否则安装核心依赖：
     ```
     pip install pandas ynab openpyxl
     ```
   - 可选（需要处理 `.rar` / `.7z` 压缩包时）：
     ```
     pip install patool
     ```
     并确保系统工具已在 PATH 中：
     - macOS: `brew install p7zip unrar`（或同类工具）
     - Ubuntu/Debian: `sudo apt-get install p7zip-full unrar`
     - Windows: 安装 7-Zip 并确保其加入 PATH

## 配置说明

应用期望的配置对象包含以下 `.env` 键：

- YNAB 设置：
    - `YNAB_API_KEY`（必填）：YNAB 个人访问令牌
    - `YNAB_BUDGET_ID`（可选）：默认预选的预算 ID
    - `YNAB_ACCOUNT_ID`（可选）：当映射不可用时的备用账户 ID
- 邮件设置：
    - `EMAIL_ADDRESS`（必填，用于邮件模式）
    - `EMAIL_PASSWORD`（在 `EMAIL_AUTH_METHOD=basic` 时必填，建议使用应用专用密码）
    - `IMAP_SERVER`（可选，默认 `imap.gmail.com`）
    - `EMAIL_IMAP_SSL` / `EMAIL_IMAP_STARTTLS`（可选布尔值；默认开启 SSL，关闭 STARTTLS）
    - `EMAIL_IMAP_ID_NAME`、`EMAIL_IMAP_ID_VERSION`、`EMAIL_IMAP_ID_VENDOR`、`EMAIL_IMAP_ID_SUPPORT_EMAIL`（可选，用于需要
      IMAP ID 信息的服务商）
    - `EMAIL_IMAP_ID_EXTRA`（可选，使用 `key=value` 逗号分隔的额外 IMAP ID 字段）
    - `EMAIL_SEARCH_HEADER_FALLBACK`（可选布尔值；开启时若 `FROM` 搜索无结果，会改用 `HEADER FROM` 重试）
    - `EMAIL_SEARCH_SAMPLE_LIMIT`（可选整数，默认 10；当两种搜索都无结果时，扫描最近邮件数量以匹配发件人）
    - `EMAIL_DISCOVER_SENDERS`（可选布尔值，默认开启；当没有匹配的发件人时记录最近邮件的实际 `From` 地址）
    - `EMAIL_DISCOVER_SAMPLE_LIMIT`（可选整数，默认 5；发现日志检查的最近邮件数量）
    - `EMAIL_SENDERS`（必填，逗号分隔的可信发件人地址列表）
    - `EMAIL_PASSPHRASE` 或 `EMAIL_PASSPHRASE_<SENDER>`（可选，不提示交互式口令时使用，启用时会打印安全警告）
    - `EMAIL_AUTH_METHOD`（可选，`basic` 或 `oauth`，默认 `basic`）
    - 使用 `oauth` 登录时还需配置：
        - `EMAIL_OAUTH_CLIENT_ID`、`EMAIL_OAUTH_TENANT_ID`、`EMAIL_OAUTH_CLIENT_SECRET`
        - `EMAIL_OAUTH_REFRESH_TOKEN`（通过 Azure 授权获取的刷新令牌）
        - `EMAIL_OAUTH_SCOPES`（可选，默认 `https://outlook.office365.com/.default`）

示例配置结构（具体文件格式取决于你的配置模块；请确保 `load_config()` 返回包含这些键的字典）：

## Usage

### 本地文件

```bash
python main.py --files /path/to/wechat_or_alipay_transactions.csv
```

### 邮件导入

```bash
python main.py
```

程序将：

1. 连接配置的 IMAP 邮箱，过滤允许的发件人。
2. 首次遇到新发件人时提示选择对应的 YNAB 预算。
3. 对加密 ZIP 附件请求一次口令（同一次运行内复用）。
4. 将解析后的交易按发件人批量上传到映射的预算，并输出每个发件人的摘要。

安全提示：

- 不会暴力破解口令；除非通过环境变量提供，否则口令仅在本次运行内驻留内存。
- 解密后的附件仅在内存或临时文件中短暂存在，处理完成后立即清除。
- 已处理邮件的 UID 会记录在 `.ynab-butler/state.json` 中，以避免重复导入；如需重新处理历史邮件，可手动删除该文件。
- Outlook / Office365 现已强制启用 OAuth2（Modern Auth）。请设置 `EMAIL_AUTH_METHOD=oauth` 及上述 OAuth 环境变量，程序会在
  IMAP 登录前安全刷新访问令牌并通过 XOAUTH2 认证。
- 若银行邮件不直接附带微信账单，而是提供形如
  `https://tenpay.wechatpay.cn/userroll/userbilldownload/downloadfilefromemail...` 的下载链接，CLI
  会自动拉取这些链接（仅允许上述域名），并将下载结果当作附件处理。
