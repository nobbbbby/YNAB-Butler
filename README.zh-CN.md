# YNAB Butler: YNAB 交易导入工具（支付宝 / 微信）

从本地文件和文件夹（CSV/Excel，包含压缩包）将个人交易导入 YNAB。
基于 IMAP 的邮件导入功能仍在规划/试验中，目前未完成，不建议使用。

上传成功后，已处理的本地文件会追加重命名为 `.done`，并且上一个自然月的文件会按目录打包归档为 `YYYY-MM.archive.zip`。

## 功能特性

- 导入来源：
  - 本地文件与整个目录（递归扫描）
  - 邮件导入（IMAP）——规划/试验中，尚未完成
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
- 读取 Excel 的支持：`openpyxl`（与 `pandas` 一起安装）
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

应用期望的配置对象包含以下结构：

- YNAB 设置 (`.env`)：
  - `api_key`（必填）：YNAB 个人访问令牌
  - `budget_id`（可选）：默认预选的预算 ID
  - `account_id`（可选）：当映射不可用时的备用 YNAB 账户 ID

基于 IMAP 的邮件导入配置将于未来版本提供，目前无需配置。

示例配置结构（具体文件格式取决于你的配置模块；请确保 `load_config()` 返回包含这些键的字典）：

## Usage
```python
python main.py --files /path/to/wechat_or_alipay_trandactions.csv
```