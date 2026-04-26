"""Tests for susops.tui.cli — dispatch() and individual cmd_* handlers.

These tests bypass the real SusOpsManager where SSH / browsers would be
required and use a tmp_path workspace instead so they are fully offline.
"""
from __future__ import annotations

import argparse
import sys
from io import StringIO
from pathlib import Path

import pytest

from susops.tui.cli import build_parser, dispatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


def run(argv: list[str], workspace: Path) -> tuple[int, str, str]:
    """Parse argv, patch workspace, run dispatch, capture stdout/stderr."""
    import susops.tui.cli as cli_mod
    from susops.facade import SusOpsManager

    args = parse(argv)
    m = SusOpsManager(workspace=workspace)

    out = StringIO()
    err = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = args.func(args, m)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue(), err.getvalue()


def run_with_manager(argv: list[str], m) -> tuple[int, str, str]:
    """Like run(), but uses a pre-created manager (allows mocking its methods)."""
    args = parse(argv)
    out = StringIO()
    err = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = args.func(args, m)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def ws(tmp_path):
    return tmp_path


@pytest.fixture
def ws_with_conn(ws):
    from susops.facade import SusOpsManager
    m = SusOpsManager(workspace=ws)
    m.add_connection("work", "user@host.example.com")
    return ws


# ---------------------------------------------------------------------------
# Parser smoke tests — verify parser builds without error
# ---------------------------------------------------------------------------

def test_parser_builds():
    p = build_parser()
    assert p is not None


def test_no_subcommand_returns_1(ws):
    args = parse([])
    code = dispatch(args)
    assert code == 1


# ---------------------------------------------------------------------------
# ps / ls — read-only, always work
# ---------------------------------------------------------------------------

def test_cmd_ps_no_connections(ws):
    code, out, _ = run(["ps"], ws)
    assert code == 3  # STOPPED
    assert "State:" in out


def test_cmd_ps_with_connection(ws_with_conn):
    code, out, _ = run(["ps"], ws_with_conn)
    assert "work" in out


def test_cmd_ls_empty(ws):
    code, out, _ = run(["ls"], ws)
    assert code == 0
    assert "pac_server_port" in out


def test_cmd_ls_with_connection(ws_with_conn):
    code, out, _ = run(["ls"], ws_with_conn)
    assert code == 0
    assert "work" in out
    assert "user@host.example.com" in out


# ---------------------------------------------------------------------------
# add-connection / rm-connection
# ---------------------------------------------------------------------------

def test_cmd_add_connection(ws):
    code, out, _ = run(["add-connection", "dev", "dev@dev.example.com"], ws)
    assert code == 0
    assert "dev" in out


def test_cmd_add_connection_duplicate(ws_with_conn):
    code, out, err = run(["add-connection", "work", "other@host.com"], ws_with_conn)
    assert code == 1
    assert "Error" in err


def test_cmd_rm_connection(ws_with_conn):
    code, out, _ = run(["rm-connection", "work"], ws_with_conn)
    assert code == 0
    assert "work" in out


def test_cmd_rm_connection_nonexistent(ws):
    code, out, err = run(["rm-connection", "ghost"], ws)
    assert code == 1
    assert "Error" in err


# ---------------------------------------------------------------------------
# add / rm — PAC hosts and port forwards
# ---------------------------------------------------------------------------

def test_cmd_add_pac_host(ws_with_conn):
    code, out, _ = run(["-c", "work", "add", "*.internal.example.com"], ws_with_conn)
    assert code == 0
    assert "*.internal.example.com" in out


def test_cmd_add_pac_host_duplicate(ws_with_conn):
    run(["-c", "work", "add", "host.com"], ws_with_conn)
    code, _, err = run(["-c", "work", "add", "host.com"], ws_with_conn)
    assert code == 1


def test_cmd_rm_pac_host(ws_with_conn):
    run(["-c", "work", "add", "host.com"], ws_with_conn)
    code, out, _ = run(["rm", "host.com"], ws_with_conn)
    assert code == 0


def test_cmd_add_local_forward(ws_with_conn):
    code, out, _ = run(
        ["-c", "work", "add", "-l", "3306", "3306"],
        ws_with_conn,
    )
    assert code == 0
    assert "3306" in out


def test_cmd_add_remote_forward(ws_with_conn):
    code, out, _ = run(
        ["-c", "work", "add", "-r", "8080", "8080"],
        ws_with_conn,
    )
    assert code == 0
    assert "8080" in out


def test_cmd_rm_local_forward(ws_with_conn):
    run(["-c", "work", "add", "-l", "3307", "3307"], ws_with_conn)
    code, out, _ = run(["rm", "-l", "3307"], ws_with_conn)
    assert code == 0


def test_cmd_rm_remote_forward(ws_with_conn):
    run(["-c", "work", "add", "-r", "8081", "8081"], ws_with_conn)
    code, out, _ = run(["rm", "-r", "8081"], ws_with_conn)
    assert code == 0


# ---------------------------------------------------------------------------
# stop — was broken (missing force kwarg); verify it now works
# ---------------------------------------------------------------------------

def test_cmd_stop_no_running_tunnel(ws_with_conn):
    """stop must not raise when nothing is running."""
    code, out, _ = run(["stop"], ws_with_conn)
    assert code == 0


def test_cmd_stop_keep_ports(ws_with_conn):
    code, out, _ = run(["stop", "--keep-ports"], ws_with_conn)
    assert code == 0


# ---------------------------------------------------------------------------
# restart — no tunnel running → should start and succeed or fail gracefully
# ---------------------------------------------------------------------------

def test_cmd_restart_no_tunnel(ws_with_conn):
    """restart with no tunnel running must not raise a TypeError."""
    args = parse(["restart"])
    from susops.facade import SusOpsManager
    m = SusOpsManager(workspace=ws_with_conn)
    # restart will call start() internally which will try SSH — catch the error
    # but it must NOT be a TypeError (which would mean a missing kwarg)
    out = StringIO()
    err = StringIO()
    sys.stdout, sys.stderr = out, err
    try:
        code = args.func(args, m)
    except TypeError as exc:
        pytest.fail(f"TypeError in cmd_restart — likely missing param: {exc}")
    except Exception:
        pass  # SSH failure is expected; the important thing is no TypeError
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__


# ---------------------------------------------------------------------------
# reset — --force skips prompt
# ---------------------------------------------------------------------------

def test_cmd_reset_force(ws_with_conn):
    code, out, _ = run(["reset", "--force"], ws_with_conn)
    assert code == 0
    assert "reset" in out.lower()


# ---------------------------------------------------------------------------
# share / fetch — requires cryptography
# ---------------------------------------------------------------------------

pytest.importorskip("cryptography", reason="cryptography package required")


def test_cmd_share_no_connection_configured(ws, tmp_path):
    test_file = tmp_path / "f.txt"
    test_file.write_text("data")
    code, out, err = run(["share", str(test_file)], ws)
    assert code == 1
    assert "Error" in err


def test_cmd_share_file_not_found(ws_with_conn):
    code, out, err = run(["share", "/nonexistent/file.txt"], ws_with_conn)
    assert code == 1
    assert "Error" in err


def test_cmd_share_and_fetch(ws_with_conn, tmp_path):
    """End-to-end: share a file via CLI then fetch it via CLI."""
    from susops.tui.cli import cmd_share, cmd_fetch
    from susops.facade import SusOpsManager

    test_file = tmp_path / "payload.txt"
    test_file.write_text("cli test payload")

    m = SusOpsManager(workspace=ws_with_conn)

    # Share
    share_args = parse(["-c", "work", "share", str(test_file)])
    out = StringIO()
    sys.stdout = out
    try:
        code = share_args.func(share_args, m)
    finally:
        sys.stdout = sys.__stdout__

    assert code == 0
    output = out.getvalue()
    # Extract port from "susops fetch <port> <password>" line
    fetch_line = next(l for l in output.splitlines() if "susops fetch" in l)
    _, _, port_str, password = fetch_line.split()
    port = int(port_str)

    # Fetch
    outfile = tmp_path / "result.txt"
    fetch_args = parse(["-c", "work", "fetch", str(port), password, str(outfile)])
    out2 = StringIO()
    sys.stdout = out2
    try:
        code2 = fetch_args.func(fetch_args, m)
    finally:
        sys.stdout = sys.__stdout__

    assert code2 == 0
    assert outfile.read_text() == "cli test payload"

    m.stop_share(port)


def test_cmd_fetch_wrong_password(ws_with_conn, tmp_path):
    from susops.core.share import ShareServer, generate_password
    from susops.facade import SusOpsManager

    test_file = tmp_path / "secret.txt"
    test_file.write_text("secret")
    pw = generate_password()

    server = ShareServer()
    info = server.start(file_path=test_file, password=pw, port=0)
    try:
        outfile = tmp_path / "out.txt"
        code, _, err = run(
            ["-c", "work", "fetch", str(info.port), "wrongpassword", str(outfile)],
            ws_with_conn,
        )
        assert code == 1
        assert "Error" in err
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Browser commands — PAC not running → error path
# ---------------------------------------------------------------------------

def test_cmd_chrome_no_pac(ws_with_conn):
    code, _, err = run(["chrome"], ws_with_conn)
    assert code == 1
    assert "PAC" in err


def test_cmd_firefox_no_pac(ws_with_conn):
    code, _, err = run(["firefox"], ws_with_conn)
    assert code == 1
    assert "PAC" in err


# ---------------------------------------------------------------------------
# ps — hierarchy characters and process tree
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_with_forward(ws_with_conn):
    from susops.facade import SusOpsManager
    from susops.core.config import PortForward
    m = SusOpsManager(workspace=ws_with_conn)
    m.add_local_forward("work", PortForward(src_port=5432, dst_port=5432))
    return ws_with_conn


@pytest.fixture
def ws_with_two_forwards(ws_with_conn):
    from susops.facade import SusOpsManager
    from susops.core.config import PortForward
    m = SusOpsManager(workspace=ws_with_conn)
    m.add_local_forward("work", PortForward(src_port=5432, dst_port=5432))
    m.add_local_forward("work", PortForward(src_port=6543, dst_port=6543))
    return ws_with_conn


def test_cmd_ps_single_forward_uses_corner(ws_with_forward):
    """Single child process uses └ (corner), never ├."""
    code, out, _ = run(["ps"], ws_with_forward)
    assert "└" in out
    assert "├" not in out
    assert "5432" in out


def test_cmd_ps_multiple_forwards_uses_tree_chars(ws_with_two_forwards):
    """Multiple children: non-last uses ├, last uses └."""
    code, out, _ = run(["ps"], ws_with_two_forwards)
    assert "├" in out
    assert "└" in out


def test_cmd_ps_shows_reconnect_daemon_running(ws):
    """● Reconnect daemon pid=N is printed when daemon_running=True."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager

    m = SusOpsManager(workspace=ws)
    fake_info = {
        "conn_children": {},
        "reconnect": {"pid": 1234, "running": True, "daemon_running": True, "thread_alive": False},
    }
    with patch.object(m, "process_info", return_value=fake_info):
        code, out, _ = run_with_manager(["ps"], m)

    assert "● Reconnect daemon" in out
    assert "pid=1234" in out


def test_cmd_ps_shows_reconnect_not_running(ws):
    """○ Reconnect is printed when daemon is not running."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager

    m = SusOpsManager(workspace=ws)
    fake_info = {
        "conn_children": {},
        "reconnect": {"pid": None, "running": False, "daemon_running": False, "thread_alive": False},
    }
    with patch.object(m, "process_info", return_value=fake_info):
        code, out, _ = run_with_manager(["ps"], m)

    assert "○ Reconnect" in out


# ---------------------------------------------------------------------------
# stop — with -c TAG
# ---------------------------------------------------------------------------

def test_cmd_stop_with_tag(ws_with_conn):
    """`susops -c work stop` must not raise and must succeed when nothing is running."""
    code, out, _ = run(["-c", "work", "stop"], ws_with_conn)
    assert code == 0


# ---------------------------------------------------------------------------
# guide — proxy setup guide
# ---------------------------------------------------------------------------

def test_cmd_guide_no_connections(ws):
    """No connections configured → exit 1, error to stderr."""
    code, out, err = run(["guide"], ws)
    assert code == 1
    assert "Error" in err


def test_cmd_guide_unknown_tag(ws_with_conn):
    """-c ghost with only 'work' configured → exit 1, error to stderr."""
    code, out, err = run(["-c", "ghost", "guide"], ws_with_conn)
    assert code == 1
    assert "Error" in err


def test_cmd_guide_running(ws_with_conn):
    """Live connection with socks_port=1080 → proxy URL in output, no warning."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager
    from susops.core.types import ConnectionStatus, StatusResult, ProcessState

    m = SusOpsManager(workspace=ws_with_conn)
    fake_status = StatusResult(
        state=ProcessState.RUNNING,
        connection_statuses=(
            ConnectionStatus(tag="work", running=True, socks_port=1080),
        ),
        pac_running=False,
        pac_port=0,
        message="",
    )
    with patch.object(m, "status", return_value=fake_status):
        code, out, err = run_with_manager(["guide"], m)

    assert code == 0
    assert "socks5h://127.0.0.1:1080" in out
    assert "Warning" not in out


def test_cmd_guide_not_running_config_port(ws):
    """Tunnel stopped but config has a saved port → warning shown, config port used."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager
    from susops.core.types import ConnectionStatus, StatusResult, ProcessState

    m = SusOpsManager(workspace=ws)
    m.add_connection("work", "user@host.example.com", socks_port=9050)

    fake_status = StatusResult(
        state=ProcessState.STOPPED,
        connection_statuses=(
            ConnectionStatus(tag="work", running=False, socks_port=0),
        ),
        pac_running=False,
        pac_port=0,
        message="",
    )
    with patch.object(m, "status", return_value=fake_status):
        code, out, err = run_with_manager(["guide"], m)

    assert code == 0
    assert "Warning" in out
    assert "socks5h://127.0.0.1:9050" in out


def test_cmd_guide_port_unknown(ws_with_conn):
    """Tunnel stopped and port is 0 in config → warning shown, <port> placeholder used."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager
    from susops.core.types import ConnectionStatus, StatusResult, ProcessState

    m = SusOpsManager(workspace=ws_with_conn)
    fake_status = StatusResult(
        state=ProcessState.STOPPED,
        connection_statuses=(
            ConnectionStatus(tag="work", running=False, socks_port=0),
        ),
        pac_running=False,
        pac_port=0,
        message="",
    )
    with patch.object(m, "status", return_value=fake_status):
        code, out, err = run_with_manager(["guide"], m)

    assert code == 0
    assert "Warning" in out
    assert "<port>" in out


def test_cmd_guide_contains_tool_sections(ws_with_conn):
    """All expected tool sections appear in the guide."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager
    from susops.core.types import ConnectionStatus, StatusResult, ProcessState

    m = SusOpsManager(workspace=ws_with_conn)
    fake_status = StatusResult(
        state=ProcessState.RUNNING,
        connection_statuses=(
            ConnectionStatus(tag="work", running=True, socks_port=1080),
        ),
        pac_running=False,
        pac_port=0,
        message="",
    )
    with patch.object(m, "status", return_value=fake_status):
        code, out, _ = run_with_manager(["guide"], m)

    assert code == 0
    for section in ("Shell", "Homebrew", "pip", "npm", "git", "curl", "wget", "apt", "Docker", "proxychains"):
        assert section in out, f"Missing section: {section}"
    # Each tool must show an alias line
    for tool_alias in (
        "alias susops-brew=", "alias susops-pip=", "alias susops-pip3=",
        "alias susops-curl=", "alias susops-wget=",
        "alias susops-npm=", "alias susops-yarn=", "alias susops-pnpm=",
        "alias susops-docker=", "alias susops-apt=",
    ):
        assert tool_alias in out, f"Missing alias: {tool_alias}"


def test_cmd_guide_default_connection(ws_with_conn):
    """Without -c, uses the first connection in config."""
    from unittest.mock import patch
    from susops.facade import SusOpsManager
    from susops.core.types import ConnectionStatus, StatusResult, ProcessState

    m = SusOpsManager(workspace=ws_with_conn)
    fake_status = StatusResult(
        state=ProcessState.RUNNING,
        connection_statuses=(
            ConnectionStatus(tag="work", running=True, socks_port=2222),
        ),
        pac_running=False,
        pac_port=0,
        message="",
    )
    with patch.object(m, "status", return_value=fake_status):
        code, out, _ = run_with_manager(["guide"], m)

    assert code == 0
    assert "work" in out
    assert "2222" in out
