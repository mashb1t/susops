"""Tests for susops.core.config — Pydantic models and YAML I/O."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from susops.core.config import (
    Connection,
    Forwards,
    PortForward,
    SusOpsConfig,
    get_connection,
    get_default_connection,
    load_config,
    save_config,
)


# ------------------------------------------------------------------ #
# Model construction
# ------------------------------------------------------------------ #

def test_port_forward_defaults():
    fw = PortForward(src_port=8080, dst_port=80)
    assert fw.src_addr == "localhost"
    assert fw.dst_addr == "localhost"
    assert fw.tag == ""


def test_port_forward_defaults_include_tcp():
    fw = PortForward(src_port=8080, dst_port=80)
    assert fw.tcp is True
    assert fw.udp is False


def test_port_forward_tcp_false_udp_false_raises():
    with pytest.raises(ValidationError, match="At least one of tcp/udp must be True"):
        PortForward(src_port=8080, dst_port=80, tcp=False, udp=False)


def test_port_forward_udp_only():
    fw = PortForward(src_port=53, dst_port=53, tcp=False, udp=True)
    assert fw.tcp is False
    assert fw.udp is True


def test_port_forward_both_protocols():
    fw = PortForward(src_port=53, dst_port=53, tcp=True, udp=True)
    assert fw.tcp is True
    assert fw.udp is True


def test_port_forward_backward_compat_no_protocol_fields():
    """Old YAML entries with no tcp/udp keys still parse with correct defaults."""
    fw = PortForward.model_validate({"src_port": 5432, "dst_port": 5432})
    assert fw.tcp is True
    assert fw.udp is False


def test_connection_defaults():
    conn = Connection(tag="work", ssh_host="user@host")
    assert conn.socks_proxy_port == 0
    assert conn.pac_hosts == []
    assert conn.forwards == Forwards()


def test_susops_config_defaults():
    cfg = SusOpsConfig()
    assert cfg.pac_server_port == 0
    assert cfg.connections == []
    assert cfg.susops_app.stop_on_quit is True


# ------------------------------------------------------------------ #
# Legacy migration
# ------------------------------------------------------------------ #

def test_legacy_string_bool_coercion():
    """Old yq-produced YAML used "1"/"0" strings for booleans."""
    cfg = SusOpsConfig.model_validate({
        "susops_app": {
            "stop_on_quit": "1",
            "ephemeral_ports": "0",
        }
    })
    assert cfg.susops_app.stop_on_quit is True
    assert cfg.susops_app.ephemeral_ports is False


def test_legacy_port_only_forward():
    """Old format used src/dst integer keys without addr fields."""
    conn = Connection.model_validate({
        "tag": "t",
        "ssh_host": "h",
        "forwards": {
            "local": [{"src": 8080, "dst": 80}],
        },
    })
    assert conn.forwards.local[0].src_port == 8080
    assert conn.forwards.local[0].dst_port == 80


# ------------------------------------------------------------------ #
# CRUD helpers
# ------------------------------------------------------------------ #

def test_get_connection():
    cfg = SusOpsConfig(connections=[Connection(tag="a", ssh_host="h")])
    assert get_connection(cfg, "a") is not None
    assert get_connection(cfg, "z") is None


def test_get_default_connection_empty():
    assert get_default_connection(SusOpsConfig()) is None


def test_get_default_connection():
    cfg = SusOpsConfig(connections=[
        Connection(tag="x", ssh_host="h1"),
        Connection(tag="y", ssh_host="h2"),
    ])
    assert get_default_connection(cfg).tag == "x"


# ------------------------------------------------------------------ #
# YAML round-trip
# ------------------------------------------------------------------ #

def test_save_and_load_roundtrip(tmp_path):
    cfg = SusOpsConfig(
        pac_server_port=8080,
        connections=[
            Connection(
                tag="work",
                ssh_host="user@work.example.com",
                socks_proxy_port=1080,
                pac_hosts=["*.internal.example.com"],
                forwards=Forwards(
                    local=[PortForward(src_port=3306, dst_port=3306, tag="db")]
                ),
            )
        ],
    )
    save_config(cfg, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.pac_server_port == cfg.pac_server_port
    assert len(loaded.connections) == 1
    conn = loaded.connections[0]
    assert conn.tag == "work"
    assert conn.ssh_host == "user@work.example.com"
    assert conn.pac_hosts == ["*.internal.example.com"]
    assert conn.forwards.local[0].src_port == 3306
    assert conn.forwards.local[0].tcp is True
    assert conn.forwards.local[0].udp is False


def test_port_forward_protocol_flags_survive_roundtrip(tmp_path):
    """tcp/udp fields must survive save_config → load_config cycle."""
    fw_tcp_only = PortForward(src_port=8080, dst_port=80, tcp=True, udp=False)
    fw_udp_only = PortForward(src_port=53, dst_port=53, tcp=False, udp=True)
    fw_both = PortForward(src_port=443, dst_port=443, tcp=True, udp=True)
    conn = Connection(
        tag="work", ssh_host="user@host",
        forwards=Forwards(local=[fw_tcp_only, fw_udp_only, fw_both])
    )
    config = SusOpsConfig(connections=[conn])
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    local = loaded.connections[0].forwards.local
    assert local[0].tcp is True and local[0].udp is False
    assert local[1].tcp is False and local[1].udp is True
    assert local[2].tcp is True and local[2].udp is True


def test_load_missing_config_returns_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.connections == []
    assert cfg.pac_server_port == 0


def test_save_config_concurrent_writes_do_not_raise(tmp_path):
    """Concurrent saves must not raise FileNotFoundError on the rename.

    Regression for: TUI rapid start/stop dispatches each RPC handler on its own
    executor thread; with a shared tmp filename, writer A would replace tmp into
    config.yaml while writer B still expected tmp to exist, raising
    FileNotFoundError on tmp.replace(path).
    """
    import threading

    cfg_a = SusOpsConfig(connections=[
        Connection(tag="a", ssh_host="u@h", socks_proxy_port=1080)
    ])
    cfg_b = SusOpsConfig(connections=[
        Connection(tag="b", ssh_host="u@h", socks_proxy_port=1081)
    ])
    save_config(cfg_a, tmp_path)

    errors: list[BaseException] = []

    def writer(cfg, n):
        try:
            for _ in range(n):
                save_config(cfg, tmp_path)
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=(cfg_a, 30))
    t2 = threading.Thread(target=writer, args=(cfg_b, 30))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, f"concurrent saves raised: {errors!r}"
    loaded = load_config(tmp_path)
    assert loaded.connections[0].tag in {"a", "b"}


def test_port_forward_enabled_defaults_true():
    fw = PortForward(src_port=80, dst_port=80)
    assert fw.enabled is True


def test_port_forward_can_be_disabled():
    fw = PortForward(src_port=80, dst_port=80, enabled=False)
    assert fw.enabled is False


def test_ssh_host_rejects_shell_metacharacters():
    """ssh_host is interpolated into shell command strings (socat EXEC), so
    whitespace and metacharacters must be rejected at the config boundary."""
    import pytest
    from susops.core.config import Connection

    for bad in ["host; rm -rf /", "host with space", "h$(whoami)", "h'x", "a|b", "h`id`"]:
        with pytest.raises(ValueError, match="ssh_host"):
            Connection(tag="t", ssh_host=bad)

    # Legitimate OpenSSH destinations must still be accepted.
    for ok in ["user@host.example.com", "myalias", "192.168.1.10", "0.0.0.0", "[::1]"]:
        assert Connection(tag="t", ssh_host=ok).ssh_host == ok


def test_forward_addr_rejects_shell_metacharacters():
    import pytest
    from susops.core.config import PortForward

    with pytest.raises(ValueError):
        PortForward(src_port=1, dst_port=2, dst_addr="db; rm -rf /")
    with pytest.raises(ValueError):
        PortForward(src_port=1, dst_port=2, src_addr="a b")

    # Documented bind values stay valid.
    for ok in ["localhost", "172.17.0.1", "0.0.0.0", "db.internal"]:
        assert PortForward(src_port=1, dst_port=2, dst_addr=ok).dst_addr == ok
