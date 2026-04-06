"""Tests for susops.core.config — Pydantic models and YAML I/O."""
from __future__ import annotations

import pytest
from pathlib import Path

from susops.core.config import (
    AppConfig,
    Connection,
    Forwards,
    PortForward,
    SusOpsConfig,
    get_connection,
    get_default_connection,
    load_config,
    save_config,
)
from susops.core.types import LogoStyle


# ------------------------------------------------------------------ #
# Model construction
# ------------------------------------------------------------------ #

def test_port_forward_defaults():
    fw = PortForward(src_port=8080, dst_port=80)
    assert fw.src_addr == "localhost"
    assert fw.dst_addr == "localhost"
    assert fw.tag == ""


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


def test_load_missing_config_returns_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.connections == []
    assert cfg.pac_server_port == 0
