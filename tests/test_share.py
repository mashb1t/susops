"""Tests for susops.core.share — encrypted file sharing."""
from __future__ import annotations

import pytest
from pathlib import Path

from susops.core.share import (
    ShareServer,
    fetch_file,
    generate_password,
    _encrypt,
    _decrypt,
    _encrypt_filename,
    _decrypt_filename,
)

pytest.importorskip("cryptography", reason="cryptography package required")


def test_generate_password_length():
    pw = generate_password(24)
    assert len(pw) == 24
    assert pw.isalnum()


def test_encrypt_decrypt_roundtrip():
    data = b"hello, world! this is test data."
    pw = "testpassword"
    encrypted = _encrypt(data, pw)
    assert encrypted[:8] == b"Salted__"
    decrypted = _decrypt(encrypted, pw)
    assert decrypted == data


def test_encrypt_filename_roundtrip():
    name = "my-secret-file.txt"
    pw = "abc123"
    enc = _encrypt_filename(name, pw)
    dec = _decrypt_filename(enc, pw)
    assert dec == name


def test_share_and_fetch(tmp_path):
    # Create a test file
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello from SusOps share!")
    pw = generate_password()

    server = ShareServer()
    info = server.start(file_path=test_file, password=pw, port=0)
    assert server.is_running()
    assert info.port > 0
    assert info.password == pw

    # Fetch the file
    outfile = tmp_path / "downloaded.txt"
    result = fetch_file(host="localhost", port=info.port, password=pw, outfile=outfile)
    assert result == outfile
    assert outfile.read_text() == "Hello from SusOps share!"

    server.stop()
    assert not server.is_running()


def test_fetch_recovers_original_filename(tmp_path):
    test_file = tmp_path / "myfile.dat"
    test_file.write_bytes(b"\x00\x01\x02\x03")
    pw = generate_password()

    server = ShareServer()
    info = server.start(file_path=test_file, password=pw, port=0)

    # Fetch without specifying outfile — should land in ~/Downloads/myfile.dat
    downloads = Path.home() / "Downloads"
    downloads.mkdir(exist_ok=True)
    expected = downloads / "myfile.dat"

    result = fetch_file(host="localhost", port=info.port, password=pw)
    assert result.name == "myfile.dat"
    assert result.read_bytes() == b"\x00\x01\x02\x03"

    server.stop()
    # Cleanup
    if result.exists():
        result.unlink()


def test_share_wrong_password(tmp_path):
    test_file = tmp_path / "secret.txt"
    test_file.write_text("secret")

    server = ShareServer()
    info = server.start(file_path=test_file, password="rightpassword", port=0)

    outfile = tmp_path / "out.txt"
    with pytest.raises(Exception):
        fetch_file(host="localhost", port=info.port, password="wrongpassword", outfile=outfile)

    server.stop()


def test_double_start_raises(tmp_path):
    test_file = tmp_path / "f.txt"
    test_file.write_text("x")
    server = ShareServer()
    server.start(file_path=test_file, password="pw", port=0)
    with pytest.raises(RuntimeError, match="already running"):
        server.start(file_path=test_file, password="pw", port=0)
    server.stop()


def test_access_count_starts_at_zero(tmp_path):
    test_file = tmp_path / "f.txt"
    test_file.write_text("data")
    server = ShareServer()
    server.start(file_path=test_file, password="pw", port=0)
    assert server.access_count == 0
    assert server.failed_count == 0
    server.stop()


def test_access_count_increments_on_successful_fetch(tmp_path):
    test_file = tmp_path / "f.txt"
    test_file.write_text("hello")
    pw = generate_password()
    server = ShareServer()
    info = server.start(file_path=test_file, password=pw, port=0)

    fetch_file(host="localhost", port=info.port, password=pw, outfile=tmp_path / "out.txt")

    assert server.access_count == 1
    assert server.failed_count == 0
    server.stop()


def test_failed_count_increments_on_wrong_password(tmp_path):
    test_file = tmp_path / "f.txt"
    test_file.write_text("secret")
    server = ShareServer()
    info = server.start(file_path=test_file, password="correct", port=0)

    with pytest.raises(Exception):
        fetch_file(host="localhost", port=info.port, password="wrong", outfile=tmp_path / "out.txt")

    assert server.failed_count == 1
    assert server.access_count == 0
    server.stop()


def test_multiple_fetches_accumulate_counts(tmp_path):
    test_file = tmp_path / "f.txt"
    test_file.write_text("data")
    pw = generate_password()
    server = ShareServer()
    info = server.start(file_path=test_file, password=pw, port=0)

    for i in range(3):
        fetch_file(host="localhost", port=info.port, password=pw, outfile=tmp_path / f"out{i}.txt")

    with pytest.raises(Exception):
        fetch_file(host="localhost", port=info.port, password="bad", outfile=tmp_path / "fail.txt")

    assert server.access_count == 3
    assert server.failed_count == 1
    server.stop()
