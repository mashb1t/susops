"""Tests for the aiohttp-based ShareServer and fetch_file."""
from __future__ import annotations

import pytest
from pathlib import Path

pytest.importorskip("cryptography", reason="cryptography package required")
pytest.importorskip("aiohttp", reason="aiohttp package required")


@pytest.fixture
def test_file(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hello from aiohttp share")
    return f


def test_share_server_starts_and_stops(test_file):
    from susops.core.share import ShareServer, generate_password
    pw = generate_password()
    srv = ShareServer()
    info = srv.start(file_path=test_file, password=pw, port=0)
    try:
        assert srv.is_running()
        assert info.port > 0
        assert info.url.startswith("http://localhost:")
        assert info.password == pw
    finally:
        srv.stop()
    assert not srv.is_running()


def test_fetch_file_roundtrip(test_file, tmp_path):
    from susops.core.share import ShareServer, fetch_file, generate_password
    pw = generate_password()
    srv = ShareServer()
    info = srv.start(file_path=test_file, password=pw, port=0)
    try:
        outfile = tmp_path / "out.txt"
        result = fetch_file(host="localhost", port=info.port, password=pw, outfile=outfile)
        assert result == outfile
        assert outfile.read_text() == "hello from aiohttp share"
    finally:
        srv.stop()


def test_two_concurrent_shares(tmp_path):
    from susops.core.share import ShareServer, fetch_file, generate_password

    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("file A content")
    f2.write_text("file B content")

    pw1 = generate_password()
    pw2 = generate_password()
    s1 = ShareServer()
    s2 = ShareServer()
    i1 = s1.start(file_path=f1, password=pw1, port=0)
    i2 = s2.start(file_path=f2, password=pw2, port=0)
    try:
        out1 = tmp_path / "out_a.txt"
        out2 = tmp_path / "out_b.txt"
        fetch_file("localhost", i1.port, pw1, outfile=out1)
        fetch_file("localhost", i2.port, pw2, outfile=out2)
        assert out1.read_text() == "file A content"
        assert out2.read_text() == "file B content"
    finally:
        s1.stop()
        s2.stop()
