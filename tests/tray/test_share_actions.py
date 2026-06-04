"""Tests for file-share do_* methods on AbstractTrayApp."""
from __future__ import annotations


def test_do_share_starts_server(tray, tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"hello world")
    tray.do_add_connection("work", "user@host")
    tray.do_share("work", str(f), password="s3cret", port=0)
    shares = tray.manager.list_shares()
    assert len(shares) == 1
    assert shares[0].file_path == str(f)


def test_do_share_produces_alert(tray, tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"hello world")
    tray.do_add_connection("work", "user@host")
    tray.do_share("work", str(f), password="s3cret", port=0)
    # On success, do_share fires a "Share Started" alert
    assert any(t == "Share Started" for t, _ in tray.alerts)


def test_do_stop_share(tray, tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"hello")
    tray.do_add_connection("work", "user@host")
    tray.do_share("work", str(f), password="s3cret", port=0)
    share = tray.manager.list_shares()[0]
    tray.do_stop_share(share.port)
    shares = tray.manager.list_shares()
    # After stop, config entry remains but is marked stopped (running=False)
    assert any(s.port == share.port and not s.running for s in shares)


def test_do_delete_share(tray, tmp_path):
    f = tmp_path / "payload.bin"
    f.write_bytes(b"hello")
    tray.do_add_connection("work", "user@host")
    tray.do_share("work", str(f), password="s3cret", port=0)
    share = tray.manager.list_shares()[0]
    tray.do_delete_share(share.port)
    assert tray.manager.list_shares() == []


def test_do_list_shares_empty(tray):
    assert tray.do_list_shares() == []
