"""Tests for susops.core.ssh — ControlMaster command building."""
from __future__ import annotations

import os

import psutil
import pytest

from susops.core import ssh as ssh_mod
from susops.core.config import Connection, PortForward
from susops.core.ssh import (
    FWD_PROCESS_PREFIX,
    SSH_PROCESS_PREFIX,
    build_master_cmd,
    find_master_pid,
    socket_path,
)


@pytest.fixture
def conn():
    return Connection(
        tag="test",
        ssh_host="user@host.example.com",
        socks_proxy_port=1080,
    )


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def test_socket_path(conn, workspace):
    p = socket_path(conn.tag, workspace)
    assert p == workspace / "sockets" / "test.sock"


def test_build_master_cmd_socks(conn, workspace):
    sock = socket_path(conn.tag, workspace)
    cmd = build_master_cmd(conn, sock)
    assert cmd[0] == "ssh"
    assert "-D" in cmd
    assert "1080" in cmd
    assert "-N" in cmd
    assert str(sock) in " ".join(cmd)
    assert "ControlMaster=yes" in " ".join(cmd)
    # Forwards are registered via ssh -O forward — never in master cmd args
    assert "-L" not in cmd
    assert "-R" not in cmd


def test_build_master_cmd_includes_ssh_host(conn, workspace):
    sock = socket_path(conn.tag, workspace)
    cmd = build_master_cmd(conn, sock)
    assert conn.ssh_host in cmd


def test_build_master_cmd_no_forwards_regardless_of_config(workspace):
    """Master cmd never contains -L/-R regardless of configured forwards."""
    from susops.core.config import Forwards
    conn = Connection(
        tag="test",
        ssh_host="user@host.example.com",
        socks_proxy_port=1080,
        forwards=Forwards(
            local=[PortForward(src_port=3306, dst_port=3306, dst_addr="db.internal", enabled=True, tcp=True)],
            remote=[PortForward(src_port=8080, dst_port=8080, enabled=True, tcp=True)],
        ),
    )
    sock = socket_path(conn.tag, workspace)
    cmd = build_master_cmd(conn, sock)
    assert "-L" not in cmd
    assert "-R" not in cmd


def test_ssh_process_prefix():
    assert SSH_PROCESS_PREFIX == "susops-ssh"


def test_fwd_process_prefix():
    assert FWD_PROCESS_PREFIX == "susops-fwd"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="requires POSIX uids")
def test_find_master_pid_strict_match(monkeypatch, workspace):
    """find_master_pid matches only an ssh process owning the EXACT ControlPath
    token + ControlMaster=yes + our uid — never a substring or foreign owner."""
    sock = str(socket_path("test", workspace))
    my_uid = os.getuid()

    class _UID:
        def __init__(self, real):
            self.real = real

    class _Proc:
        def __init__(self, pid, name, cmdline, uid):
            self.info = {"pid": pid, "name": name, "cmdline": cmdline, "uids": _UID(uid)}

    procs = [
        # the real master — exact tokens, our uid
        _Proc(111, "ssh", ["ssh", "-N", "-T", "-o", "ControlMaster=yes",
                            "-o", f"ControlPath={sock}", "user@h"], my_uid),
        # path appears only as a substring of an unrelated arg → must NOT match
        _Proc(222, "ssh", ["ssh", f"--note=see {sock} later", "ControlMaster=yes"], my_uid),
        # right argv, wrong process name
        _Proc(333, "python", ["python", "ControlMaster=yes", f"ControlPath={sock}"], my_uid),
        # right argv, foreign uid
        _Proc(444, "ssh", ["ssh", "ControlMaster=yes", f"ControlPath={sock}"], my_uid + 1),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(procs))
    assert find_master_pid("test", workspace) == 111

    # No matching process at all → None.
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(procs[1:]))
    assert find_master_pid("test", workspace) is None
