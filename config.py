# Configuration loader for environment variables
import os
from typing import Dict

from dotenv import load_dotenv


def load_config() -> dict:
    load_dotenv()
    senders_env = os.getenv('EMAIL_SENDERS', '')
    if senders_env:
        sender_list = [s.strip() for s in senders_env.split(',') if s.strip()]
    else:
        sender_list = [
            'service@mail.alipay.com',
            'alipay@service.alipay.com',
            'wechatpay@tencent.com',
            'wechatpay@service.wechat.com',
        ]
    oauth_scopes_raw = os.getenv('EMAIL_OAUTH_SCOPES', 'https://outlook.office365.com/.default')
    oauth_scopes = [scope.strip() for scope in oauth_scopes_raw.split(',') if scope.strip()]

    def _env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {'1', 'true', 'yes', 'on'}

    def _imap_id() -> Dict[str, str]:
        id_pairs = {
            'name': os.getenv('EMAIL_IMAP_ID_NAME', 'YNAB Butler'),
            'version': os.getenv('EMAIL_IMAP_ID_VERSION', '1.0.0'),
            'vendor': os.getenv('EMAIL_IMAP_ID_VENDOR', 'ynab-butler'),
            'support-email': os.getenv('EMAIL_IMAP_ID_SUPPORT_EMAIL',
                                       os.getenv('EMAIL_ADDRESS', 'support@example.com')),
        }
        extra = os.getenv('EMAIL_IMAP_ID_EXTRA', '')
        for pair in extra.split(','):
            if not pair or '=' not in pair:
                continue
            key, value = pair.split('=', 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                id_pairs[key] = value
        return {k: v for k, v in id_pairs.items() if v}

    return {
        'email': {
            'email': os.getenv('EMAIL_ADDRESS'),
            'password': os.getenv('EMAIL_PASSWORD'),
            'imap_server': os.getenv('IMAP_SERVER', 'imap.gmail.com'),
            'use_ssl': _env_bool('EMAIL_IMAP_SSL', True),
            'starttls': _env_bool('EMAIL_IMAP_STARTTLS', False),
            'header_search_fallback': _env_bool('EMAIL_SEARCH_HEADER_FALLBACK', True),
            'search_sample_limit': int(os.getenv('EMAIL_SEARCH_SAMPLE_LIMIT', '10') or '10'),
            'auth_method': os.getenv('EMAIL_AUTH_METHOD', 'basic').lower(),
            'oauth': {
                'authority': os.getenv('EMAIL_OAUTH_AUTHORITY'),
                'tenant_id': os.getenv('EMAIL_OAUTH_TENANT_ID'),
                'client_id': os.getenv('EMAIL_OAUTH_CLIENT_ID'),
                'client_secret': os.getenv('EMAIL_OAUTH_CLIENT_SECRET'),
                'refresh_token': os.getenv('EMAIL_OAUTH_REFRESH_TOKEN'),
                'scopes': oauth_scopes,
            },
            'imap_id': _imap_id(),
            'discover_senders': _env_bool('EMAIL_DISCOVER_SENDERS', True),
            'discover_sample': int(os.getenv('EMAIL_DISCOVER_SAMPLE_LIMIT', '5') or '5'),
            'senders': sender_list,
        },
        'ynab': {
            'api_key': os.getenv('YNAB_API_KEY'),
            'budget_id': os.getenv('YNAB_BUDGET_ID'),
            'account_id': os.getenv('YNAB_ACCOUNT_ID')
        }
    }
