"""Tests for susops.core.pac — PAC generation and HTTP server."""
from __future__ import annotations

import urllib.request

import pytest

from susops.core.config import Connection, SusOpsConfig
from susops.core.pac import PacServer, generate_pac, write_pac_file


@pytest.fixture
def config_with_hosts():
    return SusOpsConfig(
        pac_server_port=0,
        connections=[
            Connection(
                tag="work",
                ssh_host="user@work.example.com",
                socks_proxy_port=1080,
                pac_hosts=[
                    "*.internal.example.com",
                    "10.0.0.0/8",
                    "exact.host.com",
                ],
            )
        ],
    )


def test_generate_pac_contains_socks(config_with_hosts):
    pac = generate_pac(config_with_hosts)
    assert "SOCKS5 127.0.0.1:1080" in pac or "SOCKS 127.0.0.1:1080" in pac


def test_generate_pac_wildcard_rule(config_with_hosts):
    pac = generate_pac(config_with_hosts)
    assert "shExpMatch" in pac
    assert "*.internal.example.com" in pac


def test_generate_pac_cidr_rule(config_with_hosts):
    pac = generate_pac(config_with_hosts)
    assert "isInNet" in pac
    assert "10.0.0.0" in pac


def test_generate_pac_exact_host(config_with_hosts):
    pac = generate_pac(config_with_hosts)
    assert "exact.host.com" in pac


def test_generate_pac_empty_config():
    pac = generate_pac(SusOpsConfig())
    assert "FindProxyForURL" in pac
    assert "DIRECT" in pac


def test_write_pac_file(tmp_path, config_with_hosts):
    path = write_pac_file(config_with_hosts, tmp_path)
    assert path.exists()
    content = path.read_text()
    assert "FindProxyForURL" in content


def test_pac_server_start_stop(tmp_path, config_with_hosts):
    pac_path = write_pac_file(config_with_hosts, tmp_path)
    server = PacServer()
    assert not server.is_running()

    server.start(0, pac_path)
    assert server.is_running()
    port = server.get_port()
    assert port > 0

    # Fetch the PAC file
    url = f"http://localhost:{port}/susops.pac"
    with urllib.request.urlopen(url, timeout=5) as resp:
        content = resp.read().decode()
    assert "FindProxyForURL" in content

    server.stop()
    assert not server.is_running()


def test_pac_server_reload(tmp_path, config_with_hosts):
    pac_path = write_pac_file(config_with_hosts, tmp_path)
    server = PacServer()
    server.start(0, pac_path)

    # Write a new PAC file and reload
    new_config = SusOpsConfig(connections=[
        Connection(
            tag="new", ssh_host="h", socks_proxy_port=2080,
            pac_hosts=["reload-test.internal"],
        )
    ])
    new_path = write_pac_file(new_config, tmp_path)
    server.reload(new_path)

    port = server.get_port()
    with urllib.request.urlopen(f"http://localhost:{port}/susops.pac", timeout=5) as resp:
        content = resp.read().decode()
    assert "reload-test.internal" in content
    assert "2080" in content

    server.stop()
