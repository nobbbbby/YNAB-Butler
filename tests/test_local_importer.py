import os
import zipfile
from datetime import datetime, timedelta

import pytest

from importers.local_importer import process_local_files, archive_last_month


def _make_file(path, content=b"data", mtime: datetime | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


def _last_month_range():
    today = datetime.now().date().replace(day=1)
    last_month_end = today - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return last_month_start, last_month_end


def test_process_local_files_directory_recursion_and_skip_rules(tmp_path: pytest.TempPathFactory):
    base = tmp_path
    # Create nested structure
    f1 = _make_file(base / "a.csv", b"a1")
    f2 = _make_file(base / "b.done", b"skip")
    f3 = _make_file(base / "c.archive", b"skip")
    sub = base / "sub"
    f4 = _make_file(sub / "d.xlsx", b"xlsx")

    # Create an archive containing a csv
    archive_path = base / "pack.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("inside.csv", "x,y\n1,2\n")

    # Create a monthly archive that must be skipped entirely
    skip_archive = base / "2025-07.archive.zip"
    with zipfile.ZipFile(skip_archive, "w") as zf:
        zf.writestr("ignored.csv", "a,b\n")

    files = process_local_files([str(base)])

    # Should include full paths for normal files f1 and f4; skip f2, f3; also include archive inner name
    identifiers = {ident for ident, _ in files}

    assert str(f1) in identifiers
    assert str(f4) in identifiers
    assert str(f2) not in identifiers  # .done skipped
    assert str(f3) not in identifiers  # .archive skipped
    # Archive yields inner filename, not full path
    assert "inside.csv" in identifiers
    # Ensure skip monthly archive
    assert "ignored.csv" not in identifiers


def _build_encrypted_zip(path, password: str) -> None:
    pyzipper = pytest.importorskip("pyzipper")
    with pyzipper.AESZipFile(
            path,
            mode="w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr("secure.csv", "date,amount\n2025-01-01,10\n")


def test_process_local_files_handles_encrypted_zip(tmp_path: pytest.TempPathFactory):
    archive_path = tmp_path / "secure.zip"
    password = "secret123"
    _build_encrypted_zip(archive_path, password)

    calls = []

    def resolver(identifier: str, inner: str, attempt: int) -> str:
        calls.append((identifier, inner, attempt))
        return password

    files = process_local_files([str(archive_path)], passphrase_resolver=resolver)
    assert len(files) == 1
    name, content = files[0]
    assert name == "secure.csv"
    assert b"2025-01-01" in content
    # Ensure resolver was invoked once and cached
    assert calls == [(str(archive_path), "secure.csv", 0)]


def test_process_local_files_skips_encrypted_zip_without_passphrase(tmp_path: pytest.TempPathFactory):
    archive_path = tmp_path / "secure.zip"
    password = "secret123"
    _build_encrypted_zip(archive_path, password)

    def resolver(identifier: str, inner: str, attempt: int):
        return None

    files = process_local_files([str(archive_path)], passphrase_resolver=resolver)
    assert files == []


def test_archive_last_month_creates_zip_with_only_last_month_files(tmp_path: pytest.TempPathFactory):
    base = tmp_path
    last_start, last_end = _last_month_range()

    # Files with different mtimes
    lm_file1 = _make_file(base / "lm1.csv", b"1", mtime=datetime.combine(last_start, datetime.min.time()))
    lm_file2 = _make_file(base / "sub" / "lm2.xlsx", b"2", mtime=datetime.combine(last_end, datetime.min.time()))
    lm_skip_done = _make_file(base / "lm3.csv.done", b"3", mtime=datetime.combine(last_start, datetime.min.time()))

    # This month file should not be archived
    this_month = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tm_file = _make_file(base / "tm.csv", b"now", mtime=this_month)

    # Pre-existing archive should be ignored by selection logic and not re-added; also shouldn't break
    pre_archive_name = f"{last_start.strftime('%Y-%m')}.archive.zip"
    pre_archive = base / pre_archive_name
    with zipfile.ZipFile(pre_archive, "w") as zf:
        zf.writestr("placeholder.txt", "x")

    # Run archiver
    archive_last_month([str(base)])

    # An archive for last month should exist (may have been overwritten with new content)
    assert pre_archive.exists()

    # Inspect its contents
    with zipfile.ZipFile(pre_archive, "r") as zf:
        names = set(zf.namelist())

    # The archived paths should be relative to base
    expect_rel1 = os.path.relpath(str(lm_file1), str(base))
    expect_rel2 = os.path.relpath(str(lm_file2), str(base))

    assert expect_rel1 in names
    assert expect_rel2 in names

    # Skipped and non-last-month files should not be included
    assert os.path.relpath(str(lm_skip_done), str(base)) not in names
    assert os.path.relpath(str(tm_file), str(base)) not in names
