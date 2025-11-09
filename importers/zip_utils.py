from __future__ import annotations

import logging
import os
import random
import zlib
from io import BytesIO
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pyzipper

try:
    _BAD_ZIPFILE_ERRORS: Tuple[type[BaseException], ...] = (pyzipper.BadZipFile,)  # type: ignore[attr-defined]
except AttributeError:
    _BAD_ZIPFILE_ERRORS = ()

if hasattr(pyzipper, "zipfile") and hasattr(pyzipper.zipfile, "BadZipFile"):
    _BAD_ZIPFILE_ERRORS = _BAD_ZIPFILE_ERRORS + (pyzipper.zipfile.BadZipFile,)

try:
    from zipfile import BadZipFile as _StdBadZipFile
except ImportError:  # pragma: no cover - stdlib always available but keep defensive
    _StdBadZipFile = None
else:
    _BAD_ZIPFILE_ERRORS = _BAD_ZIPFILE_ERRORS + (_StdBadZipFile,)

_ZIP_DECOMPRESSION_ERRORS = (zlib.error,) + _BAD_ZIPFILE_ERRORS

PassphraseResolver = Callable[[str, str, int], Optional[str]]

_ENV_PASSPHRASE_WARNED: set[str] = set()

# Brute-force state: per-identifier generator and attempt counters
_BRUTEFORCE_STATE: Dict[str, Tuple[Iterable[int], int]] = {}

# Track identifiers for which we already warned about unlimited brute-force mode
_UNLIMITED_WARNED: set[str] = set()

# Maximum brute-force attempts per identifier (to avoid extremely long runs)
# Set to 0 or a negative number to allow unlimited attempts (until generator is exhausted).
_MAX_BRUTEFORCE_ATTEMPTS = int(os.getenv('ZIP_MAX_BRUTEFORCE_ATTEMPTS', '200'))


def _generate_all_six_digit_random_generator():
    """
    Generate all unique six-digit numbers (000000 to 999999) in random order.
    True memory-efficient generator using virtual shuffling.

    Yields:
        int: Six-digit numbers in random order (may include leading zeros)
    """
    n = 1_000_000  # Total count of 6-digit numbers
    remaining = list(range(n))  # Indices only

    for i in range(n):
        # Pick random index from remaining
        j = random.randint(i, n - 1)
        remaining[i], remaining[j] = remaining[j], remaining[i]
        yield remaining[i]


def sanitize_identifier(identifier: str) -> str:
    cleaned = ''.join(ch.upper() if ch.isalnum() else '_' for ch in identifier)
    return cleaned or 'IDENTIFIER'


def build_env_keys(identifier: str, templates: Iterable[str]) -> List[str]:
    sanitized = sanitize_identifier(identifier)
    keys: List[str] = []
    for template in templates:
        if '{identifier}' in template:
            keys.append(template.replace('{identifier}', sanitized))
        else:
            keys.append(template)
    return keys


def get_env_passphrase(identifier: str, templates: Iterable[str]) -> Optional[str]:
    """Return a passphrase from environment variables using the provided templates."""
    for key in build_env_keys(identifier, templates):
        value = os.getenv(key)
        if value:
            if key not in _ENV_PASSPHRASE_WARNED:
                logging.warning(
                    "Using passphrase from environment variable %s; store secrets securely.",
                    key,
                )
                _ENV_PASSPHRASE_WARNED.add(key)
            return value.strip()
    return None


def next_six_digit_candidate(identifier: str) -> Optional[str]:
    """Return the next six-digit candidate string for the given identifier.

    Uses a randomized iterator per sanitized identifier and a configurable cap
    `_MAX_BRUTEFORCE_ATTEMPTS` to avoid excessively long runs. Set env
    `ZIP_MAX_BRUTEFORCE_ATTEMPTS` to 0 or a negative number to allow unlimited
    attempts (iteration continues until the six‑digit generator is exhausted).
    Generator covers the full range 000000-999999 without repeats.
    """
    key = sanitize_identifier(identifier)
    # Initialize iterator and counter if missing
    if key not in _BRUTEFORCE_STATE:
        _BRUTEFORCE_STATE[key] = (_generate_all_six_digit_random_generator(), 0)
        if _MAX_BRUTEFORCE_ATTEMPTS <= 0 and key not in _UNLIMITED_WARNED:
            logging.warning(
                "Unlimited ZIP brute-force attempts enabled (ZIP_MAX_BRUTEFORCE_ATTEMPTS=%s) for %s. This may be slow.",
                _MAX_BRUTEFORCE_ATTEMPTS,
                key,
            )
            _UNLIMITED_WARNED.add(key)
    it, count = _BRUTEFORCE_STATE[key]
    # Respect cap only when positive
    if _MAX_BRUTEFORCE_ATTEMPTS > 0 and count >= _MAX_BRUTEFORCE_ATTEMPTS:
        return None
    try:
        num = next(it)
    except StopIteration:
        return None
    else:
        count += 1
        _BRUTEFORCE_STATE[key] = (it, count)
        return f"{num:06d}"


def _read_encrypted_member(
        zf: pyzipper.AESZipFile,
        info,
        identifier: str,
        context: str,
        cache_key: str,
        resolver: PassphraseResolver,
        cache: Dict[str, str],
        label: str,
) -> Optional[bytes]:
    attempts = 0
    while True:
        passphrase = cache.get(cache_key)
        if passphrase is None:
            passphrase = resolver(identifier, context, attempts)
            if not passphrase:
                logging.warning(
                    "No passphrase supplied for %s; skipping %s.",
                    label,
                    info.filename,
                )
                return None
            cache[cache_key] = passphrase
        if attempts > 0 and attempts % 100 == 0:
            logging.info(
                "Brute-force attempt %d for %s: trying passphrase %s",
                attempts,
                label,
                passphrase,
            )

        if passphrase == '036383':
            a = 1
        pwd_bytes = passphrase.encode('utf-8')
        try:
            # Performance optimization: Pass password directly to open() and read() methods
            # instead of setting zf.pwd. This avoids redundant PBKDF2 key derivation.
            # For brute-force attempts (attempts > 0), validate password by reading only
            # a small chunk first to fail fast on incorrect passwords.
            if attempts > 0 and info.file_size > 1024:
                # During brute-force, validate with a small chunk first
                with zf.open(info, pwd=pwd_bytes) as f:
                    # Read a small portion (1KB) to validate the password
                    # If password is wrong, this will fail quickly without decrypting the entire file
                    chunk = f.read(1024)
                    # Password is correct, read the rest
                    return chunk + f.read()

            # For first attempt (likely user-provided password) or small files,
            # read the entire file directly
            return zf.read(info, pwd=pwd_bytes)
        except RuntimeError as exc:
            if attempts == 0:
                # Log first failed attempt as warning (likely user-provided wrong password)
                logging.warning(
                    f"Incorrect passphrase for %s when reading %s: %s",
                    label,
                    info.filename,
                    exc,
                )
            else:
                # During brute-force, use debug level to avoid massive logs
                logging.debug(
                    f"Incorrect passphrase for %s when reading %s: %s {passphrase}",
                    label,
                    info.filename,
                    exc,
                )
            cache.pop(cache_key, None)
            attempts += 1
            # Continue attempting until resolver indicates no more candidates
            # by returning a falsy value on the next call.
        except _ZIP_DECOMPRESSION_ERRORS as exc:
            # Decompression or CRC errors indicate the passphrase failed integrity checks after decrypting.
            if attempts == 0:
                logging.warning(
                    f"Decompression failed for %s when reading %s (likely incorrect passphrase): %s",
                    label,
                    info.filename,
                    exc,
                )
            else:
                logging.debug(
                    f"Decompression failed for %s when reading %s: %s",
                    label,
                    info.filename,
                    exc,
                )
            cache.pop(cache_key, None)
            attempts += 1
            # Retry with next passphrase candidate
        except Exception as exc:
            logging.error("Failed to read encrypted member %s: %s", info.filename, exc)
            return None


def _extract_zip(
        opener: Callable[[], pyzipper.AESZipFile],
        identifier: str,
        resolver: PassphraseResolver,
        cache: Dict[str, str],
        *,
        label: str,
        cache_key: Optional[str] = None,
        allowed_extensions: Optional[Iterable[str]] = None,
        context_provider: Optional[Callable[[pyzipper.ZipInfo], str]] = None,
) -> List[Tuple[str, bytes]]:
    extracted: List[Tuple[str, bytes]] = []
    cache_key = cache_key or identifier.lower()
    try:
        with opener() as zf:
            for info in zf.infolist():
                name = info.filename
                if not name or name.endswith('/'):
                    continue
                if allowed_extensions:
                    lower_name = name.lower()
                    if not any(lower_name.endswith(ext) for ext in allowed_extensions):
                        continue
                data: Optional[bytes] = None
                context = context_provider(info) if context_provider else info.filename
                if info.flag_bits & 0x1:
                    data = _read_encrypted_member(
                        zf,
                        info,
                        identifier,
                        context,
                        cache_key,
                        resolver,
                        cache,
                        label,
                    )
                else:
                    try:
                        data = zf.read(info)
                    except Exception as exc:
                        logging.warning(
                            "Failed to read %s from %s: %s",
                            name,
                            label,
                            exc,
                        )
                if data:
                    extracted.append((name, data))
    except RuntimeError as exc:
        logging.warning("ZIP processing failed for %s: %s", label, exc)
    except Exception as exc:
        logging.error("Unexpected error reading %s: %s", label, exc, exc_info=True)
    return extracted


def extract_zip_bytes(
        archive_bytes: bytes,
        identifier: str,
        parent_label: str,
        resolver: PassphraseResolver,
        cache: Dict[str, str],
        *,
        cache_key: Optional[str] = None,
        allowed_extensions: Optional[Iterable[str]] = None,
        context_provider: Optional[Callable[[pyzipper.ZipInfo], str]] = None,
) -> List[Tuple[str, bytes]]:
    return _extract_zip(
        lambda: pyzipper.AESZipFile(BytesIO(archive_bytes)),
        identifier,
        resolver,
        cache,
        label=parent_label,
        cache_key=cache_key,
        allowed_extensions=allowed_extensions,
        context_provider=context_provider,
    )


def extract_zip_file(
        archive_path: str,
        identifier: str,
        resolver: PassphraseResolver,
        cache: Dict[str, str],
        *,
        cache_key: Optional[str] = None,
        allowed_extensions: Optional[Iterable[str]] = None,
        context_provider: Optional[Callable[[pyzipper.ZipInfo], str]] = None,
) -> List[Tuple[str, bytes]]:
    label = os.path.basename(archive_path) or archive_path
    return _extract_zip(
        lambda: pyzipper.AESZipFile(archive_path),
        identifier,
        resolver,
        cache,
        label=label,
        cache_key=cache_key,
        allowed_extensions=allowed_extensions,
        context_provider=context_provider,
    )
