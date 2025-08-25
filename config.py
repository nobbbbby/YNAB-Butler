# Configuration loader for environment variables
import os
from dotenv import load_dotenv

def load_config() -> dict:
    load_dotenv()
    return {
        'email': {
            'email': os.getenv('EMAIL_ADDRESS'),
            'password': os.getenv('EMAIL_PASSWORD'),
            'imap_server': os.getenv('IMAP_SERVER', 'imap.gmail.com'),
            'senders': [
                'alipay@service.alipay.com',
                'wechatpay@tencent.com',
                'wechatpay@service.wechat.com',
            ]
        },
        'ynab': {
            'api_key': os.getenv('YNAB_API_KEY'),
            'budget_id': os.getenv('YNAB_BUDGET_ID'),
            'account_id': os.getenv('YNAB_ACCOUNT_ID')
        }
    }
