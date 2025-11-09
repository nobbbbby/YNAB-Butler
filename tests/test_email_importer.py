from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List

import pytest

from importers.email_importer import (
    EmailStateStore,
    _log_recent_senders,
    connect_email,
    download_attachments,
)

pyzipper = pytest.importorskip("pyzipper")


class DummyAddr:
    def __init__(self, sender: str):
        mailbox, host = sender.split('@', 1)
        self.mailbox = mailbox.encode()
        self.host = host.encode()


class DummyEnvelope:
    def __init__(self, sender: str):
        self.from_ = [DummyAddr(sender)]


class DummyIMAPClient:
    """Minimal IMAP client mock supporting search/fetch/add_flags."""

    def __init__(self, messages: Dict[int, bytes], sender: str):
        self._messages = messages
        self._sender = sender.lower()
        self.added_flags: Dict[int, List[bytes]] = {}

    def search(self, criteria: Iterable) -> List[int]:
        # Accept ['FROM', sender] style search
        if len(criteria) >= 2 and str(criteria[0]).upper() == 'FROM':
            sender = str(criteria[1]).strip('"').lower()
            return list(self._messages.keys()) if sender == self._sender else []
        if len(criteria) >= 3 and str(criteria[0]).upper() == 'HEADER' and str(criteria[1]).upper() == 'FROM':
            sender = str(criteria[2]).strip('"').lower()
            return list(self._messages.keys()) if sender == self._sender else []
        if len(criteria) == 1 and str(criteria[0]).upper() == 'ALL':
            return list(self._messages.keys())
        return []

    def fetch(self, uids: Iterable[int], items: Iterable[str]):
        results = {}
        for uid in uids:
            if uid not in self._messages:
                continue
            record = {}
            if any(item == 'RFC822' for item in items):
                record[b'RFC822'] = self._messages[uid]
            if any(item == 'ENVELOPE' for item in items):
                record[b'ENVELOPE'] = DummyEnvelope(self._sender)
            results[uid] = record
        return results

    def add_flags(self, uid: int, flags: Iterable[bytes]) -> None:
        self.added_flags.setdefault(uid, []).extend(list(flags))


def _build_encrypted_zip(password: str) -> bytes:
    buf = BytesIO()
    with pyzipper.AESZipFile(
            buf,
            mode='w',
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(password.encode('utf-8'))
        zf.writestr('transactions.csv', 'date,amount\n2025-01-01,10')
    return buf.getvalue()


def _build_email_with_zip(sender: str, attachment_bytes: bytes, filename: str = 'report.zip') -> bytes:
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = 'finance@example.com'
    msg['Subject'] = 'Transactions'
    msg.set_content('Please see attached.')
    msg.add_attachment(
        attachment_bytes,
        maintype='application',
        subtype='zip',
        filename=filename,
    )
    return msg.as_bytes()


def test_download_attachments_handles_encrypted_zip():
    password = 'secret123'
    zip_bytes = _build_encrypted_zip(password)
    raw_email = _build_email_with_zip('Bank <bank@example.com>', zip_bytes)
    client = DummyIMAPClient({1: raw_email}, 'bank@example.com')

    def resolver(sender: str, filename: str, attempt: int) -> str:
        return password

    result = download_attachments(
        client,
        ['bank@example.com'],
        'imap|user',
        processed_uids=set(),
        passphrase_resolver=resolver,
    )

    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.filename.endswith('transactions.csv')
    assert b'2025-01-01' in attachment.content
    # Message should be marked processed and flagged as seen
    assert '1' in result.processed_uids
    assert 1 in client.added_flags


def test_download_attachments_skips_when_passphrase_missing():
    password = 'secret123'
    zip_bytes = _build_encrypted_zip(password)
    raw_email = _build_email_with_zip('Bank <bank@example.com>', zip_bytes)
    client = DummyIMAPClient({7: raw_email}, 'bank@example.com')

    def resolver(sender: str, filename: str, attempt: int):
        return None  # simulate user skipping

    result = download_attachments(
        client,
        ['bank@example.com'],
        'imap|user',
        processed_uids=set(),
        passphrase_resolver=resolver,
    )
    assert not result.attachments
    assert not result.processed_uids  # message should remain for future runs
    assert '7' in result.skipped_uids


def test_email_state_store_persists(tmp_path: Path):
    state_path = tmp_path / 'state.json'
    store = EmailStateStore(path=state_path)
    store.add_processed_uids('server|user', ['1', '2'])
    store.set_owner_budget('bank@example.com', 'budget-123')
    store.set_owner_budget('alice', 'budget-xyz')
    store.save()

    loaded = EmailStateStore(path=state_path)
    assert loaded.get_processed_uids('server|user') == {'1', '2'}
    assert loaded.get_owner_budget('bank@example.com') == 'budget-123'
    assert loaded.get_owner_budget('alice') == 'budget-xyz'


def test_log_recent_senders(caplog):
    class EnvelopeAddr:
        def __init__(self, mailbox: bytes, host: bytes):
            self.mailbox = mailbox
            self.host = host

    class Envelope:
        def __init__(self, mailbox: bytes, host: bytes):
            self.from_ = [EnvelopeAddr(mailbox, host)]

    class DiscoveryIMAP:
        def __init__(self):
            self._uids = [1, 2, 3]

        def search(self, criteria):
            if criteria == ['ALL']:
                return self._uids
            return []

        def fetch(self, uids, items):
            return {uid: {b'ENVELOPE': Envelope(b'user%d' % uid, b'example.com')} for uid in uids}

    caplog.set_level("INFO")
    _log_recent_senders(DiscoveryIMAP(), sample_size=2)
    assert "Sender discovery" in caplog.text


def test_download_attachments_header_fallback(monkeypatch):
    payload = _build_email_with_zip('Bank <bank@example.com>', _build_encrypted_zip('pw'))

    class HeaderFallbackClient(DummyIMAPClient):
        def search(self, criteria):
            if criteria == ['FROM', '"bank@example.com"']:
                return []
            if criteria == ['HEADER', 'FROM', '"bank@example.com"']:
                return super().search(['FROM', '"bank@example.com"'])
            return []

    client = HeaderFallbackClient({42: payload}, 'bank@example.com')
    result = download_attachments(
        client,
        ['bank@example.com'],
        'imap|user',
        processed_uids=set(),
        passphrase_resolver=lambda *_: 'pw',
        header_search_fallback=True,
    )
    assert result.attachments
    assert '42' in result.processed_uids


def test_download_attachments_recent_scan(monkeypatch):
    payload = _build_email_with_zip('Bank <bank@example.com>', _build_encrypted_zip('pw'))

    class RecentScanClient(DummyIMAPClient):
        def search(self, criteria):
            # Force regular searches to fail so we rely on ALL
            if len(criteria) >= 2 and str(criteria[0]).upper() == 'FROM':
                return []
            if len(criteria) >= 3 and str(criteria[0]).upper() == 'HEADER':
                return []
            return super().search(criteria)

    client = RecentScanClient({100: payload}, 'bank@example.com')
    result = download_attachments(
        client,
        ['bank@example.com'],
        'imap|user',
        processed_uids=set(),
        passphrase_resolver=lambda *_: 'pw',
        header_search_fallback=False,
        fallback_scan_limit=5,
    )
    assert result.attachments
    assert '100' in result.processed_uids


def test_download_attachments_handles_tenpay_link(monkeypatch):
    msg = EmailMessage()
    msg['From'] = 'WeChat <wechatpay@tencent.com>'
    msg['To'] = 'user@example.com'
    msg['Subject'] = 'Link'
    link = 'https://tenpay.wechatpay.cn/userroll/userbilldownload/downloadfilefromemail?a=1'
    msg.set_content(f'Please download at {link}')
    payload = msg.as_bytes()
    client = DummyIMAPClient({55: payload}, 'wechatpay@tencent.com')

    def fake_download(url: str):
        assert url == link
        return 'wechat.csv', b'data'

    monkeypatch.setattr('importers.email_importer._download_tenpay_file', fake_download)

    result = download_attachments(
        client,
        ['wechatpay@tencent.com'],
        'imap|user',
        processed_uids=set(),
    )
    assert len(result.attachments) == 1
    assert result.attachments[0].filename == 'wechat.csv'
    assert result.attachments[0].content == b'data'
    assert '55' in result.processed_uids


def test_connect_email_basic_auth(monkeypatch):
    calls = {}

    class FakeClient:
        def __init__(self, host, ssl):
            calls['init'] = (host, ssl)

        def login(self, username, password):
            calls['login'] = (username, password)

        def select_folder(self, folder):
            calls['folder'] = folder

        def id_(self, params):
            calls['imap_id'] = params

    monkeypatch.setattr('importers.email_importer.IMAPClient', FakeClient)
    client = connect_email(
        'imap.test.example',
        'user@example.com',
        'hunter2',
        auth_method='basic',
        imap_id={'name': 'client', 'version': '1.0'},
    )
    assert client is not None
    assert calls['login'] == ('user@example.com', 'hunter2')
    assert calls['folder'] == 'INBOX'
    assert calls['imap_id'] == {'name': 'client', 'version': '1.0'}


def test_connect_email_starttls(monkeypatch):
    calls = {}

    class FakeClient:
        def __init__(self, host, ssl):
            calls['init'] = (host, ssl)

        def starttls(self):
            calls['starttls'] = True

        def login(self, username, password):
            calls['login'] = (username, password)

        def select_folder(self, folder):
            calls['folder'] = folder

    monkeypatch.setattr('importers.email_importer.IMAPClient', FakeClient)
    client = connect_email(
        'imap.starttls.example',
        'user@example.com',
        'pw',
        auth_method='basic',
        use_ssl=False,
        starttls=True,
    )
    assert client is not None
    assert calls['init'] == ('imap.starttls.example', False)
    assert calls['starttls'] is True


def test_connect_email_oauth(monkeypatch):
    calls = {}

    class FakeClient:
        def __init__(self, host, ssl):
            calls['init'] = (host, ssl)

        def oauth2_login(self, username, token):
            calls['oauth_login'] = (username, token)

        def select_folder(self, folder):
            calls['folder'] = folder

    monkeypatch.setattr('importers.email_importer.IMAPClient', FakeClient)
    monkeypatch.setattr('importers.email_importer._acquire_access_token', lambda _: 'token-abc')

    client = connect_email(
        'imap.outlook.com',
        'user@example.com',
        None,
        auth_method='oauth',
        oauth_config={'client_id': 'x'},
    )
    assert client is not None
    assert calls['oauth_login'] == ('user@example.com', 'token-abc')
