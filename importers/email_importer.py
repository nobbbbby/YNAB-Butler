from __future__ import annotations

import email
import json
import logging
import os
import re
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

import requests
from imapclient import IMAPClient


def _ensure_imap_file_setter() -> None:
    ""
    "Work around Python 3.13+ removing IMAP4.file assignment support."""
    try:
        import imaplib  # pylint: disable=import-outside-toplevel
    except Exception:  # pragma: no cover - stdlib import guard
        return

    file_prop = getattr(imaplib.IMAP4, 'file', None)
    if isinstance(file_prop, property):
        def _file_getter(self):  # type: ignore
            return getattr(self, '_file', None)

        def _file_setter(self, value):  # type: ignore
            setattr(self, '_file', value)

        imaplib.IMAP4.file = property(_file_getter, _file_setter)  # type: ignore[attr-defined]


_ensure_imap_file_setter()

from importers.zip_utils import (  # noqa: E402
    PassphraseResolver,
    extract_zip_bytes,
    get_env_passphrase,
    next_six_digit_candidate,
)

LOGGER = logging.getLogger(__name__)

ATTACHMENT_EXTENSIONS = {'.csv', '.xlsx', '.xls'}
ZIP_EXTENSIONS = {'.zip'}
STATE_DIR = Path(__file__).resolve().parent.parent / '.ynab-butler'
STATE_FILE = STATE_DIR / 'state.json'
EMAIL_PASSPHRASE_TEMPLATES = [
    "EMAIL_PASSPHRASE_{identifier}",
    "EMAIL_PASSPHRASE",
]
TENPAY_LINK_PATTERN = re.compile(
    r"https://tenpay\.wechatpay\.cn/userroll/userbilldownload/downloadfilefromemail[^\s\"'<>\)]+",
    re.IGNORECASE,
)
TENPAY_TIMEOUT = 30


@dataclass
class EmailAttachment:
    sender: str
    subject: str
    filename: str
    content: bytes
    message_uid: str


@dataclass
class EmailFetchResult:
    attachments: List[EmailAttachment]
    processed_uids: Set[str]
    skipped_uids: Set[str]
    warnings: List[str]


class EmailStateStore:
    """Persist mailbox message UIDs, owner budget mappings, and account mappings."""

    def __init__(self, path: Path = STATE_FILE):
        self.path = Path(path)
        self._data: Dict[str, Dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open('r', encoding='utf-8') as handle:
                    self._data = json.load(handle)
            except Exception as exc:
                LOGGER.warning("Failed to load email state (%s); starting fresh.", exc)
                self._data = {}
        self._data.setdefault('mailboxes', {})
        # Remove legacy sender_budget mappings; owner_budget replaces them.
        legacy = self._data.pop('sender_budget', None)
        if legacy:
            LOGGER.debug("Ignoring legacy sender_budget entries; migrate to owner_budget if needed.")
        self._data.setdefault('owner_budget', {})
        self._data.setdefault('account_mappings', {})

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix('.tmp')
        with tmp_path.open('w', encoding='utf-8') as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)
        self._dirty = False

    def get_processed_uids(self, mailbox_key: str) -> Set[str]:
        mailbox = self._data['mailboxes'].get(mailbox_key, {})
        return set(mailbox.get('processed_uids', []))

    def add_processed_uids(self, mailbox_key: str, uids: Iterable[str]) -> None:
        mailbox = self._data['mailboxes'].setdefault(mailbox_key, {})
        stored = mailbox.setdefault('processed_uids', [])
        updated = False
        for uid in uids:
            if uid not in stored:
                stored.append(uid)
                updated = True
        if updated:
            self._dirty = True

    def get_owner_budget(self, owner: str) -> Optional[str]:
        if not owner:
            return None
        return self._data['owner_budget'].get(owner.lower())

    def set_owner_budget(self, owner: str, budget_id: str) -> None:
        if not owner:
            return
        key = owner.lower()
        if self._data['owner_budget'].get(key) == budget_id:
            return
        self._data['owner_budget'][key] = budget_id
        self._dirty = True

    @staticmethod
    def _account_key(budget_id: str, account_name: str) -> str:
        return f"{budget_id}:{account_name.strip().lower()}"

    def get_account_mapping(self, budget_id: str, account_name: str) -> Optional[str]:
        if not budget_id or not account_name:
            return None
        return self._data['account_mappings'].get(self._account_key(budget_id, account_name))

    def set_account_mapping(self, budget_id: str, account_name: str, account_id: str) -> None:
        if not (budget_id and account_name and account_id):
            return
        mapping = self._data.setdefault('account_mappings', {})
        key = self._account_key(budget_id, account_name)
        if mapping.get(key) == account_id:
            return
        mapping[key] = account_id
        self._dirty = True

    def get_all_account_mappings(self) -> Dict[str, str]:
        return dict(self._data.get('account_mappings', {}))

    def replace_account_mappings(self, new_mappings: Dict[str, str]) -> None:
        self._data['account_mappings'] = dict(new_mappings)
        self._dirty = True


def _acquire_access_token(oauth_config: Optional[Dict[str, object]]) -> Optional[str]:
    """Exchange the configured refresh token for an OAuth2 access token."""
    if not oauth_config:
        LOGGER.error("OAuth2 authentication requested but no oauth configuration found.")
        return None

    client_id = oauth_config.get('client_id')
    tenant_id = oauth_config.get('tenant_id')
    client_secret = oauth_config.get('client_secret')
    refresh_token = oauth_config.get('refresh_token')
    authority = oauth_config.get('authority') or (
        f"https://login.microsoftonline.com/{tenant_id}" if tenant_id else None
    )
    scopes = oauth_config.get('scopes') or ['https://outlook.office365.com/.default']

    missing = [
        name
        for name, value in (
            ('EMAIL_OAUTH_CLIENT_ID', client_id),
            ('EMAIL_OAUTH_TENANT_ID', tenant_id),
            ('EMAIL_OAUTH_CLIENT_SECRET', client_secret),
            ('EMAIL_OAUTH_REFRESH_TOKEN', refresh_token),
        )
        if not value
    ]
    if missing:
        LOGGER.error("OAuth2 authentication missing configuration: %s", ", ".join(missing))
        return None
    if not authority:
        LOGGER.error("Unable to derive OAuth2 authority URL; set EMAIL_OAUTH_AUTHORITY or tenant ID.")
        return None
    if isinstance(scopes, str):
        scopes = [scopes]
    elif isinstance(scopes, Sequence):
        scopes = [str(scope) for scope in scopes if str(scope).strip()]
    else:
        scopes = ['https://outlook.office365.com/.default']

    try:
        import msal  # type: ignore
    except ImportError:  # pragma: no cover - guarded by dependency
        LOGGER.error(
            "msal package is required for OAuth2 authentication. Install it or set EMAIL_AUTH_METHOD=basic."
        )
        return None

    cca = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=authority,
        client_credential=client_secret,
    )
    LOGGER.info("Requesting OAuth2 access token for scope(s): %s", ", ".join(scopes))
    try:
        result = cca.acquire_token_by_refresh_token(refresh_token, scopes=scopes)
    except Exception as exc:
        LOGGER.error("Failed to exchange refresh token for access token: %s", exc)
        return None

    access_token = result.get('access_token')
    if access_token:
        return access_token
    error_desc = result.get('error_description') or result.get('error') or 'unknown-error'
    LOGGER.error("OAuth2 token request failed: %s", error_desc)
    return None


def connect_email(
        imap_server: str,
        email_address: str,
        password: Optional[str],
        auth_method: str = 'basic',
        oauth_config: Optional[Dict[str, object]] = None,
        *,
        use_ssl: bool = True,
        starttls: bool = False,
        imap_id: Optional[Dict[str, str]] = None,
) -> Optional[IMAPClient]:
    """Connect to IMAP server and return the client instance."""
    auth_label = (auth_method or 'basic').lower()
    LOGGER.info(
        "Connecting to IMAP host %s as %s using %s auth (ssl=%s, starttls=%s)",
        imap_server,
        email_address,
        auth_label,
        use_ssl,
        starttls,
    )
    try:
        if starttls and use_ssl:
            LOGGER.warning("starttls requested while EMAIL_IMAP_SSL=true; disabling SSL for STARTTLS negotiation.")
            use_ssl = False
        client = IMAPClient(imap_server, ssl=use_ssl)
        if starttls:
            try:
                client.starttls()
                LOGGER.info("STARTTLS negotiated for %s", imap_server)
            except Exception as exc:
                LOGGER.error("Failed to negotiate STARTTLS with %s: %s", imap_server, exc)
                return None
        if auth_label == 'oauth':
            access_token = _acquire_access_token(oauth_config)
            if not access_token:
                LOGGER.error("Unable to acquire OAuth2 access token; aborting IMAP login.")
                return None
            client.oauth2_login(email_address, access_token)
        else:
            if not password:
                LOGGER.error("EMAIL_PASSWORD must be set when using basic authentication.")
                return None
            client.login(email_address, password)
        if imap_id:
            try:
                client.id_(imap_id)
                LOGGER.debug(
                    "Sent IMAP ID parameters: %s",
                    ', '.join(f"{k}={v}" for k, v in imap_id.items()),
                )
            except Exception as exc:
                LOGGER.warning("Failed to send IMAP ID parameters: %s", exc)
        client.select_folder('INBOX')
        LOGGER.info("Connected to %s as %s", imap_server, email_address)
        return client
    except Exception as exc:
        LOGGER.error(
            "IMAP login failed for %s as %s (%s): %s",
            imap_server,
            email_address,
            exc.__class__.__name__,
            exc,
        )
        LOGGER.debug(
            "Verify IMAP credentials / OAuth tokens and mailbox permissions depending on EMAIL_AUTH_METHOD."
        )
        return None


def _default_passphrase_resolver(sender: str, filename: str, attempt: int) -> Optional[str]:
    """Resolve passphrase: environment first, then six-digit random candidates.

    Non-interactive by default (no prompt) to support unattended email ingestion.
    """
    if attempt == 0:
        env_passphrase = get_env_passphrase(sender, EMAIL_PASSPHRASE_TEMPLATES)
        if env_passphrase:
            return env_passphrase
    # Try next six-digit candidate tied to sender identity
    return next_six_digit_candidate(sender)


def _log_recent_senders(mail: IMAPClient, sample_size: int = 5) -> None:
    """Fetch recent message envelopes to help users discover actual sender addresses."""
    if sample_size <= 0:
        return
    try:
        all_uids = mail.search(['ALL'])
    except Exception as exc:
        LOGGER.warning("Sender discovery search failed: %s", exc)
        return
    if not all_uids:
        LOGGER.info("Sender discovery: mailbox is empty.")
        return
    sample_uids = all_uids[-sample_size:]
    try:
        fetched = mail.fetch(sample_uids, ['ENVELOPE'])
    except Exception as exc:
        LOGGER.warning("Sender discovery fetch failed: %s", exc)
        return
    discovered: List[str] = []
    for uid in sample_uids:
        envelope = fetched.get(uid, {}).get(b'ENVELOPE')
        if not envelope:
            continue
        addresses = getattr(envelope, 'from_', []) or []
        if not addresses:
            continue
        addr = addresses[0]
        mailbox = getattr(addr, 'mailbox', b'') or b''
        host = getattr(addr, 'host', b'') or b''
        if isinstance(mailbox, bytes):
            mailbox = mailbox.decode(errors='ignore')
        if isinstance(host, bytes):
            host = host.decode(errors='ignore')
        mailbox = (mailbox or '').strip()
        host = (host or '').strip()
        if mailbox and host:
            discovered.append(f"{mailbox}@{host}".lower())
    if discovered:
        unique = list(dict.fromkeys(discovered))
        LOGGER.info(
            "Sender discovery: recent From addresses include %s. Add them to EMAIL_SENDERS if needed.",
            ", ".join(unique),
        )
    else:
        LOGGER.info("Sender discovery: no From addresses extracted from recent messages.")


def _scan_recent_uids_for_sender(
        mail: IMAPClient,
        sender: str,
        sample_size: int,
) -> List[int]:
    """Fetch recent message UIDs and return those whose From matches sender."""
    if sample_size <= 0:
        return []
    try:
        all_uids = mail.search(['ALL'])
    except Exception as exc:
        LOGGER.debug("Recent scan search failed for %s: %s", sender, exc)
        return []
    if not all_uids:
        return []
    sample_uids = all_uids[-sample_size:]
    try:
        fetched = mail.fetch(sample_uids, ['ENVELOPE'])
    except Exception as exc:
        LOGGER.debug("Recent scan fetch failed for %s: %s", sender, exc)
        return []
    normalized_sender = sender.strip().strip('"').lower()
    matches: List[int] = []
    for uid in sample_uids:
        envelope = fetched.get(uid, {}).get(b'ENVELOPE')
        if not envelope:
            continue
        addresses = getattr(envelope, 'from_', []) or []
        if not addresses:
            continue
        addr = addresses[0]
        mailbox = getattr(addr, 'mailbox', b'') or b''
        host = getattr(addr, 'host', b'') or b''
        if isinstance(mailbox, bytes):
            mailbox = mailbox.decode(errors='ignore')
        if isinstance(host, bytes):
            host = host.decode(errors='ignore')
        candidate = f"{mailbox}@{host}".strip().lower()
        if candidate == normalized_sender:
            matches.append(uid)
    if matches:
        LOGGER.info(
            "Sender %s matched %d message(s) via recent-envelope fallback scan.",
            sender,
            len(matches),
        )
    return matches


def _extract_tenpay_links(message: Message) -> List[str]:
    links: List[str] = []
    for part in message.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        content_type = part.get_content_type()
        if content_type not in ('text/plain', 'text/html'):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or 'utf-8'
        try:
            text = payload.decode(charset, errors='replace')
        except (LookupError, UnicodeDecodeError):
            text = payload.decode('utf-8', errors='replace')
        found = TENPAY_LINK_PATTERN.findall(text)
        for raw in found:
            clean = raw.strip(" '\"\t\r\n,><)")
            if clean.lower().startswith("https://tenpay.wechatpay.cn/"):
                links.append(clean)
    # Preserve order but drop duplicates
    seen: Set[str] = set()
    unique: List[str] = []
    for link in links:
        key = link.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
    return unique


def _filename_from_disposition(disposition: str) -> Optional[str]:
    if not disposition:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?("?)([^";]+)\1', disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(2))
    return None


def _download_tenpay_file(url: str) -> Tuple[str, bytes]:
    resp = requests.get(url, timeout=TENPAY_TIMEOUT)
    resp.raise_for_status()
    filename = _filename_from_disposition(resp.headers.get('Content-Disposition', ''))
    if not filename:
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or "wechat_download.bin"
    return filename, resp.content


def _quote_imap_value(value: str) -> str:
    escaped = value.replace('"', r'\"')
    if escaped.startswith('"') and escaped.endswith('"'):
        return escaped
    return f'"{escaped}"'


def download_attachments(
        mail: IMAPClient,
        senders: List[str],
        mailbox_key: str,
        processed_uids: Set[str],
        passphrase_resolver: Optional[PassphraseResolver] = None,
        header_search_fallback: bool = False,
        fallback_scan_limit: int = 0,
) -> EmailFetchResult:
    """Fetch new attachments from configured senders while preventing duplicate processing."""
    if not mail:
        LOGGER.error("IMAP client not connected")
        return EmailFetchResult([], set(), set(), ["no-imap-connection"])

    resolver = passphrase_resolver or _default_passphrase_resolver
    passphrase_cache: Dict[str, str] = {}
    attachments: List[EmailAttachment] = []
    processed: Set[str] = set()
    skipped: Set[str] = set()
    warnings: List[str] = []
    downloaded_links: Set[str] = set()

    unique_senders = [s for s in {s.lower() for s in senders} if s]
    LOGGER.info("Email ingestion searching %d allowed senders in mailbox %s.", len(unique_senders), mailbox_key)
    if not unique_senders:
        LOGGER.warning("No sender addresses configured.")
        return EmailFetchResult([], set(), set(), warnings)

    for sender in unique_senders:
        search_value = _quote_imap_value(sender)
        try:
            # IMAPClient search uses uppercase keys
            message_ids = mail.search(['FROM', search_value])
            LOGGER.debug("Sender %s returned %d message(s).", sender, len(message_ids))
            if not message_ids and header_search_fallback:
                LOGGER.debug("Sender %s had no matches with FROM; retrying HEADER search.", sender)
                message_ids = mail.search(['HEADER', 'FROM', search_value])
                if message_ids:
                    LOGGER.debug(
                        "Sender %s matched %d message(s) via HEADER FROM fallback.",
                        sender,
                        len(message_ids),
                    )
            if not message_ids and fallback_scan_limit:
                message_ids = _scan_recent_uids_for_sender(mail, sender, fallback_scan_limit)
        except Exception as exc:
            LOGGER.error("Failed to search mailbox %s for sender %s: %s", mailbox_key, sender, exc)
            warnings.append(f"search-failed:{sender}")
            continue

        for uid in message_ids:
            uid_str = str(uid)
            if uid_str in processed_uids:
                LOGGER.debug("Skipping UID %s for sender %s (already processed).", uid_str, sender)
                continue
            try:
                fetch_data = mail.fetch([uid], ['RFC822', 'FLAGS', 'ENVELOPE'])
            except Exception as exc:
                LOGGER.error("Failed to fetch message %s: %s", uid_str, exc)
                warnings.append(f"fetch-failed:{uid_str}")
                continue
            msg_blob = fetch_data.get(uid, {}).get(b'RFC822')
            msg_warnings: List[str] = []
            if not msg_blob:
                msg_warnings.append("missing RFC822 payload")
                skipped.add(uid_str)
                LOGGER.warning("Message %s issues: %s", uid_str, "; ".join(msg_warnings))
                continue
            message: Message = email.message_from_bytes(msg_blob)
            subject = _decode_header(message.get('subject', ''))
            sender_addr = _extract_email(message.get('from', '')).lower() or sender
            LOGGER.debug("Processing message UID %s from %s - %s", uid_str, sender_addr, subject)

            extracted_any = False
            for part in message.walk():
                if part.get_content_maintype() == 'multipart':
                    continue
                filename = part.get_filename()
                if not filename:
                    continue
                filename = _decode_header(filename)
                lower_name = filename.lower()
                if not (lower_name.endswith(tuple(ATTACHMENT_EXTENSIONS)) or lower_name.endswith(
                        tuple(ZIP_EXTENSIONS))):
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    LOGGER.debug("Attachment %s had no payload; skipping.", filename)
                    continue
                if lower_name.endswith(tuple(ZIP_EXTENSIONS)):
                    extracted = extract_zip_bytes(
                        payload,
                        sender_addr,
                        filename,
                        resolver,
                        passphrase_cache,
                        cache_key=sender_addr.lower(),
                        allowed_extensions=ATTACHMENT_EXTENSIONS,
                        context_provider=lambda _info, parent=filename: parent,
                    )
                    if extracted:
                        for inner_name, data in extracted:
                            attachments.append(EmailAttachment(sender_addr, subject, inner_name, data, uid_str))
                        extracted_any = True
                    else:
                        warnings.append(f"zip-skip:{uid_str}:{filename}")
                        msg_warnings.append(f"zip-skip:{filename}")
                else:
                    attachments.append(EmailAttachment(sender_addr, subject, filename, payload, uid_str))
                    extracted_any = True

            link_urls = _extract_tenpay_links(message)
            for link in link_urls:
                link_key = link.lower()
                if link_key in downloaded_links:
                    continue
                try:
                    dl_name, dl_bytes = _download_tenpay_file(link)
                except Exception as exc:
                    LOGGER.debug("Failed to download Tenpay link for UID %s: %s", uid_str, exc)
                    warnings.append(f"link-download-failed:{uid_str}")
                    msg_warnings.append("link-download-failed")
                    continue
                lower_dl = dl_name.lower()
                downloaded_links.add(link_key)
                if lower_dl.endswith(tuple(ZIP_EXTENSIONS)):
                    extracted = extract_zip_bytes(
                        dl_bytes,
                        sender_addr,
                        dl_name,
                        resolver,
                        passphrase_cache,
                        cache_key=sender_addr.lower(),
                        allowed_extensions=ATTACHMENT_EXTENSIONS,
                        context_provider=lambda _info, parent=dl_name: parent,
                    )
                    if extracted:
                        for inner_name, data in extracted:
                            attachments.append(EmailAttachment(sender_addr, subject, inner_name, data, uid_str))
                        extracted_any = True
                    else:
                        warnings.append(f"link-zip-skip:{uid_str}:{dl_name}")
                        msg_warnings.append(f"link-zip:{dl_name}")
                else:
                    attachments.append(EmailAttachment(sender_addr, subject, dl_name, dl_bytes, uid_str))
                    extracted_any = True

            if extracted_any:
                processed.add(uid_str)
                try:
                    mail.add_flags(uid, [b'\\Seen'])
                except Exception:
                    LOGGER.debug("Unable to mark message %s as seen.", uid_str, exc_info=True)
            else:
                skipped.add(uid_str)

            if msg_warnings:
                LOGGER.warning("Message %s issues: %s", uid_str, "; ".join(msg_warnings))

    return EmailFetchResult(attachments, processed, skipped, warnings)


def _decode_header(header_value: str) -> str:
    if not header_value:
        return ""
    try:
        decoded = email.header.decode_header(header_value)
        parts: List[str] = []
        for content, enc in decoded:
            if isinstance(content, bytes):
                encoding = enc or 'utf-8'
                try:
                    parts.append(content.decode(encoding, errors='replace'))
                except (UnicodeDecodeError, LookupError):
                    parts.append(content.decode('utf-8', errors='replace'))
            else:
                parts.append(str(content))
        return ' '.join(parts)
    except Exception:
        return header_value


def _extract_email(header_value: str) -> str:
    import re

    if not header_value:
        return ""
    match = re.search(r'<([^>]+)>', header_value)
    if match:
        return match.group(1)
    return header_value.strip()
