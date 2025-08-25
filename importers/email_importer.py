import imaplib
import email
from email.header import decode_header
import logging
from typing import List, Tuple, Optional

def connect_email(imap_server: str, email_address: str, password: str) -> Optional[imaplib.IMAP4_SSL]:
    """Connect to email server and return the connection object."""
    try:
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(email_address, password)
        mail.select('inbox')
        logging.info("Successfully connected to email server")
        return mail
    except Exception as e:
        logging.error(f"Failed to connect to email: {str(e)}")
        return None

def download_attachments(mail: imaplib.IMAP4_SSL, senders: List[str]) -> List[Tuple[str, bytes]]:
    """Download email attachments from specified senders."""
    if not mail:
        logging.error("Not connected to email server")
        return []
    attachments = []
    try:
        search_query = ' OR '.join([f'FROM "{sender}"' for sender in senders])
        status, messages = mail.search(None, search_query)
        if status != 'OK' or not messages[0]:
            logging.info("No matching emails found")
            return []
        for num in messages[0].split():
            status, msg_data = mail.fetch(num, '(RFC822)')
            if status != 'OK':
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_header(msg['subject'])
            sender = _extract_email(msg['from'])
            logging.info(f"Processing email from {sender} - {subject}")
            for part in msg.walk():
                if part.get_content_maintype() == 'multipart':
                    continue
                filename = part.get_filename()
                if not filename:
                    continue
                filename = _decode_header(filename)
                if not filename.lower().endswith(('.csv', '.xlsx', '.xls', '.zip')):
                    continue
                file_data = part.get_payload(decode=True)
                if not file_data:
                    continue
                # Handle zipped files
                if filename.lower().endswith('.zip'):
                    import zipfile, io
                    with zipfile.ZipFile(io.BytesIO(file_data)) as zf:
                        for zipped_file in zf.namelist():
                            if zipped_file.lower().endswith(('.csv', '.xlsx', '.xls')):
                                attachments.append((zipped_file, zf.read(zipped_file)))
                else:
                    attachments.append((filename, file_data))
    except Exception as e:
        logging.error(f"Error processing emails: {str(e)}")
    return attachments

def _decode_header(header):
    if header is None:
        return ""
    try:
        decoded = decode_header(header)
        parts = []
        for content, encoding in decoded:
            if isinstance(content, bytes):
                try:
                    parts.append(content.decode(encoding or 'utf-8', errors='replace'))
                except (UnicodeDecodeError, LookupError):
                    parts.append(content.decode('utf-8', errors='replace'))
            else:
                parts.append(str(content))
        return ' '.join(parts)
    except Exception:
        return str(header)

def _extract_email(header):
    import re
    if not header:
        return ""
    match = re.search(r'<([^>]+)>', header)
    if match:
        return match.group(1)
    return header
