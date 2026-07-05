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


def test_build_master_cmd_no_jump_by_default(conn, workspace):
    cmd = build_master_cmd(conn, socket_path(conn.tag, workspace))
    assert "-J" not in cmd


def test_build_master_cmd_has_stream_local_bind_unlink(conn, workspace):
    cmd = build_master_cmd(conn, socket_path(conn.tag, workspace))
    assert "StreamLocalBindUnlink=yes" in " ".join(cmd)


def test_build_fwd_spec_tcp_and_sockets():
    from susops.core.ssh import build_fwd_spec
    from susops.core.config import PortForward
    # TCP↔TCP (unchanged): addr:port:addr:port
    f = PortForward(src_addr="localhost", src_port=5432, dst_addr="db", dst_port=5432)
    assert build_fwd_spec(f, "local") == "localhost:5432:db:5432"
    assert build_fwd_spec(f, "remote") == "localhost:5432:db:5432"
    # TCP → unix (postgres)
    f = PortForward(src_port=5432, dst_socket="/var/run/pg.sock")
    assert build_fwd_spec(f, "local") == "localhost:5432:/var/run/pg.sock"
    # unix → TCP
    f = PortForward(src_socket="/tmp/l.sock", dst_addr="localhost", dst_port=80)
    assert build_fwd_spec(f, "local") == "/tmp/l.sock:localhost:80"
    # unix → unix (docker)
    f = PortForward(src_socket="/tmp/docker.sock", dst_socket="/var/run/docker.sock")
    assert build_fwd_spec(f, "local") == "/tmp/docker.sock:/var/run/docker.sock"


def test_local_socket_for_direction():
    from susops.core.ssh import local_socket_for
    from susops.core.config import PortForward
    # -L: local side is the source
    f = PortForward(src_socket="/tmp/l.sock", dst_addr="h", dst_port=80)
    assert local_socket_for(f, "local") == "/tmp/l.sock"
    assert local_socket_for(f, "remote") == ""
    # -R: local side is the destination
    f = PortForward(src_socket="/remote.sock", dst_addr="localhost", dst_port=80)
    assert local_socket_for(f, "remote") == ""  # dst is TCP here
    f = PortForward(src_addr="localhost", src_port=8080, dst_socket="/tmp/local.sock")
    assert local_socket_for(f, "remote") == "/tmp/local.sock"


def test_socket_forward_active(tmp_path):
    import os
    import socket as _socket
    import tempfile
    from susops.core.ssh import socket_forward_active
    missing = tmp_path / "nope.sock"
    assert socket_forward_active(str(missing)) is False
    regular = tmp_path / "regular"
    regular.write_text("x")
    assert socket_forward_active(str(regular)) is False  # exists but not a socket
    # Bind under a short dir — AF_UNIX paths must be < ~104 bytes and pytest's
    # tmp_path is often longer than that.
    d = tempfile.mkdtemp(prefix="sus")
    real = os.path.join(d, "r.sock")
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(real)
    try:
        assert socket_forward_active(real) is True
    finally:
        s.close()
        os.unlink(real)
        os.rmdir(d)


def test_build_master_cmd_adds_jump_host(workspace):
    conn = Connection(tag="t", ssh_host="user@target", socks_proxy_port=1080,
                      jump_host="user@bastion")
    cmd = build_master_cmd(conn, socket_path(conn.tag, workspace))
    assert "-J" in cmd
    assert cmd[cmd.index("-J") + 1] == "user@bastion"
    # jump flag precedes the destination host
    assert cmd.index("-J") < cmd.index(conn.ssh_host)


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


def test_cancel_forward_missing_socket_is_noop(tmp_path, monkeypatch):
    """cancel_forward returns without invoking ssh when the socket is gone."""
    from susops.core.config import Connection, PortForward

    calls = []
    monkeypatch.setattr("susops.core.ssh.subprocess.run",
                        lambda cmd, **kw: calls.append(cmd))
    conn = Connection(tag="t", ssh_host="user@h")
    fw = PortForward(src_port=5432, dst_port=5432, dst_addr="db.internal", tag="pg")

    ssh_mod.cancel_forward(conn, fw, "local", tmp_path)  # no socket yet
    assert calls == []


def test_cancel_forward_invokes_ssh_O_cancel(tmp_path, monkeypatch):
    """With a live socket, cancel_forward runs ssh -O cancel with the right spec."""
    from susops.core.config import Connection, PortForward

    calls = []
    monkeypatch.setattr("susops.core.ssh.subprocess.run",
                        lambda cmd, **kw: calls.append(cmd))
    conn = Connection(tag="t", ssh_host="user@h")
    fw = PortForward(src_port=5432, dst_port=6432, dst_addr="db.internal", tag="pg")

    sock = socket_path("t", tmp_path)
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.touch()

    ssh_mod.cancel_forward(conn, fw, "local", tmp_path)

    assert len(calls) == 1
    argv = calls[0]
    assert argv[:3] == ["ssh", "-O", "cancel"]
    assert "-L" in argv  # local → -L
    assert "localhost:5432:db.internal:6432" in argv
    assert "user@h" in argv
