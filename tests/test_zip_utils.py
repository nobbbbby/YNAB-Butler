from pathlib import Path

import pytest

import importers.zip_utils as zip_utils
from importers.zip_utils import extract_zip_file, next_six_digit_candidate

pyzipper = pytest.importorskip("pyzipper")


def test_brute_force_decryption_on_test_archive(tmp_path: Path, monkeypatch):
    """Test brute-force decryption using next_six_digit_candidate on a test archive with known password."""
    # Create a test archive with a known 6-digit password
    test_password = "123456"
    test_archive = tmp_path / "brute_test.zip"

    # Create an encrypted zip with test data
    with pyzipper.AESZipFile(
            test_archive,
            mode='w',
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(test_password.encode('utf-8'))
        zf.writestr('test_data.csv', 'date,amount,description\n2025-01-01,100.00,Test Transaction\n')

    # Set unlimited attempts for test to allow full random enumeration
    monkeypatch.setattr(zip_utils, '_MAX_BRUTEFORCE_ATTEMPTS', 0)

    # Clear any existing state for clean test run
    if str(test_archive) in zip_utils._BRUTEFORCE_STATE:
        del zip_utils._BRUTEFORCE_STATE[str(test_archive)]

    # Track how many attempts were made
    attempts_made = []

    # Create a resolver that uses next_six_digit_candidate for brute-force
    def brute_force_resolver(identifier: str, context: str, attempt: int) -> str:
        candidate = next_six_digit_candidate(identifier)
        if candidate:
            attempts_made.append(candidate)
        return candidate if candidate else None

    # Attempt to extract the archive using brute-force
    cache = {}
    extracted = extract_zip_file(
        str(test_archive),
        str(test_archive),
        brute_force_resolver,
        cache,
    )

    # Verify extraction was successful
    assert len(extracted) > 0, "Should extract at least one file"

    # Check that we extracted the test data file
    filenames = [name for name, _ in extracted]
    assert "test_data.csv" in filenames, f"Expected test_data.csv, got: {filenames}"

    # Verify the content is correct
    for name, content in extracted:
        if name == "test_data.csv":
            assert b"Test Transaction" in content, "File should contain test transaction data"
            assert len(content) > 0, f"File {name} should have content"

    # Verify that a password was cached
    assert len(cache) > 0, "Password should be cached after successful decryption"
    assert cache[str(test_archive).lower()] == test_password, "Cached password should match the correct password"

    # Verify that brute-force found the password (it should be in the attempts)
    assert test_password in attempts_made, f"Password {test_password} should have been tried during brute-force"

    # Verify that some attempts were made
    # Note: With random enumeration, this could be anywhere from 1 to 900000 attempts
    # but statistically should be found within a reasonable number of tries
    assert len(attempts_made) > 0, "Should have made at least one attempt"


def test_decryption_with_specific_passphrase_036383():
    """Test decryption using a specific known passphrase: 036383 with existing wechat.zip archive."""
    # Use the existing wechat.zip archive from tests directory
    test_password = "036383"
    test_archive = Path(__file__).parent / "wechat.zip"

    assert test_archive.exists(), f"Test archive not found: {test_archive}"

    # Create a simple resolver that returns the specific passphrase
    def specific_passphrase_resolver(identifier: str, context: str, attempt: int) -> str:
        return test_password

    # Attempt to extract the archive
    cache = {}
    extracted = extract_zip_file(
        str(test_archive),
        str(test_archive),
        specific_passphrase_resolver,
        cache,
    )

    # Verify extraction was successful
    assert len(extracted) == 1, "Should extract exactly one file"

    # Check that we extracted the WeChat payment file
    name, content = extracted[0]
    assert name.endswith('.xlsx'), f"Expected .xlsx file, got: {name}"
    assert '微信支付账单' in name, f"Expected WeChat payment file, got: {name}"

    # Verify the content is not empty (Excel file should have content)
    assert len(content) > 0, f"File {name} should have content"
    assert len(content) > 1000, f"Excel file should be reasonably sized, got {len(content)} bytes"

    # Verify that the password was cached
    assert len(cache) > 0, "Password should be cached after successful decryption"
    assert cache[str(test_archive).lower()] == test_password, f"Cached password should be {test_password}"
