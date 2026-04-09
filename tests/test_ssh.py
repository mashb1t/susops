"""Tests for susops.core.ssh — ControlMaster command building."""
from __future__ import annotations

from pathlib import Path

import pytest

from susops.core.config import Connection, Forwards, PortForward
from susops.core.ssh import (
    FWD_PROCESS_PREFIX,
    SSH_PROCESS_PREFIX,
    build_forward_cmd,
    build_master_cmd,
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
    assert "-D" in cmd
    assert "1080" in cmd
    assert "-N" in cmd
    assert str(sock) in " ".join(cmd)
    assert "ControlMaster=yes" in " ".join(cmd)
    # Must NOT include -L or -R (those are handled by slave processes)
    assert "-L" not in cmd
    assert "-R" not in cmd


def test_build_master_cmd_includes_ssh_host(conn, workspace):
    sock = socket_path(conn.tag, workspace)
    cmd = build_master_cmd(conn, sock)
    assert conn.ssh_host in cmd


def test_build_forward_cmd_local(conn, workspace):
    fw = PortForward(src_port=3306, dst_port=3306, dst_addr="db.internal")
    sock = socket_path(conn.tag, workspace)
    cmd = build_forward_cmd(conn, fw, "local", sock)
    assert "-L" in cmd
    assert "3306" in " ".join(cmd)
    assert "db.internal" in " ".join(cmd)
    assert "-R" not in cmd
    assert str(sock) in " ".join(cmd)


def test_build_forward_cmd_remote(conn, workspace):
    fw = PortForward(src_port=8080, dst_port=8080)
    sock = socket_path(conn.tag, workspace)
    cmd = build_forward_cmd(conn, fw, "remote", sock)
    assert "-R" in cmd
    assert "8080" in " ".join(cmd)
    assert "-L" not in cmd


def test_ssh_process_prefix():
    assert SSH_PROCESS_PREFIX == "susops-ssh"


def test_fwd_process_prefix():
    assert FWD_PROCESS_PREFIX == "susops-fwd"
