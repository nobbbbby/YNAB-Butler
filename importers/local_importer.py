import logging
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from importers.zip_utils import (
    PassphraseResolver,
    extract_zip_file,
    get_env_passphrase,
    next_six_digit_candidate,
)

LOCAL_PASSPHRASE_TEMPLATES = [
    "LOCAL_ARCHIVE_PASSPHRASE_{identifier}",
    "LOCAL_ARCHIVE_PASSPHRASE",
    "EMAIL_PASSPHRASE_{identifier}",
    "EMAIL_PASSPHRASE",
]


def _default_passphrase_resolver(identifier: str, inner_name: str, attempt: int) -> Optional[str]:
    """Resolve passphrase: environment first, then six-digit random candidates.

    Non-interactive by default (no prompt) to support unattended local ingestion.
    """
    if attempt == 0:
        env_passphrase = get_env_passphrase(identifier, LOCAL_PASSPHRASE_TEMPLATES)
        if env_passphrase:
            return env_passphrase
    # Try next six-digit candidate tied to identifier (e.g., folder or user)
    return next_six_digit_candidate(identifier)

def extract_archive(file_path: str, extract_dir: str) -> List[Tuple[str, bytes]]:
    """Extract supported archive files and return list of (filename, content) tuples.
    Note: returned filename is the inner file's name (no on-disk path), so these cannot be renamed later.
    """
    extracted_files = []
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    try:
        if ext == '.zip':
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
                for root, _, files in os.walk(extract_dir):
                    for file in files:
                        fpath = os.path.join(root, file)
                        with open(fpath, 'rb') as f:
                            extracted_files.append((file, f.read()))
        else:
            # Fall back to patool for other archive types if available
            import patoolib
            patoolib.extract_archive(file_path, outdir=extract_dir, interactive=False)
            for root, _, files in os.walk(extract_dir):
                for file in files:
                    fpath = os.path.join(root, file)
                    with open(fpath, 'rb') as f:
                        extracted_files.append((file, f.read()))
        return extracted_files
    except Exception as e:
        logging.error(f"Error extracting archive {file_path}: {str(e)}")
        return []


def _is_skipped_file(path: str) -> bool:
    """Return True if the file should be skipped based on suffix rules."""
    name = os.path.basename(path).lower()
    if name.endswith('.done'):
        return True
    if name.endswith('.archive'):
        return True
    if name.endswith('.archive.zip'):
        return True
    if name.startswith('test_'):
        return True
    return False


def _iter_candidate_files(inputs: Iterable[str]) -> Iterable[str]:
    """Yield candidate file paths from files or directories recursively, applying skip rules."""
    for inp in inputs:
        if not os.path.exists(inp):
            logging.error(f"Path not found: {inp}")
            continue
        if os.path.isdir(inp):
            for root, _, files in os.walk(inp):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    if _is_skipped_file(fpath):
                        continue
                    yield fpath
        else:
            if not _is_skipped_file(inp):
                yield inp


def process_local_files(
        file_paths: List[str],
        passphrase_resolver: Optional[PassphraseResolver] = None,
) -> List[Tuple[str, bytes]]:
    """Process transaction files from local filesystem (files or directories), including compressed archives.
    Returns list of (identifier, content) where identifier is the full file path for local files or inner filename for archives.
    """
    if not file_paths:
        logging.warning("No file paths provided")
        return []
    resolver = passphrase_resolver or _default_passphrase_resolver
    passphrase_cache: Dict[str, str] = {}
    all_files: List[Tuple[str, bytes]] = []
    temp_dir = None
    try:
        for file_path in _iter_candidate_files(file_paths):
            filename = os.path.basename(file_path)
            _, ext = os.path.splitext(filename)
            ext = ext.lower()
            is_archive = ext in ['.zip', '.rar', '.7z']
            # Skip special archive we produce
            if filename.lower().endswith('.archive.zip'):
                continue
            if is_archive:
                logging.info(f"Processing archive: {file_path}")
                if ext == '.zip':
                    extracted_files = extract_zip_file(
                        file_path,
                        file_path,
                        resolver,
                        passphrase_cache,
                        cache_key=file_path.lower(),
                        context_provider=lambda info: info.filename,
                    )
                else:
                    temp_dir = tempfile.mkdtemp()
                    extracted_files = extract_archive(file_path, temp_dir)
                for ext_filename, content in extracted_files:
                    logging.info(f"  Processing file from archive: {ext_filename}")
                    all_files.append((ext_filename, content))
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    temp_dir = None
            else:
                logging.info(f"Processing local file: {file_path}")
                with open(file_path, 'rb') as f:
                    content = f.read()
                # Use full path as identifier so caller can rename after success
                all_files.append((file_path, content))
        return all_files
    except Exception as e:
        logging.error(f"Error during file processing: {str(e)}")
        return []
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def archive_last_month(dirs: List[str]) -> None:
    """Archive last month's files in the given directories into a zip named 'YYYY-MM.archive.zip'.
    Rules:
    - Only consider regular files directly or in subdirectories of the provided dirs.
    - Skip any file ending with .done, .archive, or .archive.zip.
    - Group by top-level provided directory; produce one archive per provided dir, containing relative paths.
    - Uses file modification time to determine last-month membership.
    """
    if not dirs:
        return
    # Compute last month range
    today = datetime.now().date().replace(day=1)
    last_month_end = today - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    def is_last_month(path: str) -> bool:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path)).date()
            return last_month_start <= mtime <= last_month_end
        except Exception:
            return False

    # Normalize directories (only keep directories)
    norm_dirs = []
    for d in dirs:
        if os.path.isdir(d):
            norm_dirs.append(os.path.abspath(d))
        elif os.path.isfile(d):
            norm_dirs.append(os.path.abspath(os.path.dirname(d)))
    norm_dirs = sorted(set(norm_dirs))

    for base_dir in norm_dirs:
        # Collect files to archive relative to base_dir
        files_to_archive = []
        for root, _, files in os.walk(base_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                if _is_skipped_file(fpath):
                    continue
                if is_last_month(fpath):
                    rel = os.path.relpath(fpath, base_dir)
                    files_to_archive.append((fpath, rel))
        if not files_to_archive:
            continue
        archive_name = f"{last_month_start.strftime('%Y-%m')}.archive.zip"
        archive_path = os.path.join(base_dir, archive_name)
        try:
            with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                for fpath, rel in files_to_archive:
                    zf.write(fpath, arcname=rel)
            logging.info(f"Archived {len(files_to_archive)} file(s) from {base_dir} into {archive_path}")
        except Exception as e:
            logging.error(f"Failed to create archive {archive_path}: {e}")
