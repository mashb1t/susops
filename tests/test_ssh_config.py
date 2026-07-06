"""Tests for susops.core.ssh_config — host + forward parsing."""
from __future__ import annotations

from susops.core.ssh_config import get_ssh_hosts, parse_ssh_forwards

_CONFIG = """\
Host bastion
    HostName bastion.example.com

Host db
    HostName db.internal
    LocalForward 5432 db.internal:5432
    LocalForward 127.0.0.1:6379 cache:6379
    RemoteForward 8080 localhost:8080
    DynamicForward 1080

Host *
    ForwardAgent yes
"""


def _write(tmp_path, text):
    p = tmp_path / "config"
    p.write_text(text)
    return p


def test_get_ssh_hosts_skips_wildcards(tmp_path):
    hosts = get_ssh_hosts(_write(tmp_path, _CONFIG))
    assert hosts == ["bastion", "db"]


def test_parse_forwards_for_host(tmp_path):
    fw = parse_ssh_forwards("db", _write(tmp_path, _CONFIG))
    assert fw["local"] == [
        {"src_addr": "localhost", "src_port": 5432,
         "dst_addr": "db.internal", "dst_port": 5432},
        {"src_addr": "127.0.0.1", "src_port": 6379,
         "dst_addr": "cache", "dst_port": 6379},
    ]
    assert fw["remote"] == [
        {"src_addr": "localhost", "src_port": 8080,
         "dst_addr": "localhost", "dst_port": 8080},
    ]
    assert fw["dynamic"] == [1080]


def test_parse_forwards_unknown_host_is_empty(tmp_path):
    fw = parse_ssh_forwards("nope", _write(tmp_path, _CONFIG))
    assert fw == {"local": [], "remote": [], "dynamic": []}


def test_parse_forwards_missing_file():
    fw = parse_ssh_forwards("db", __import__("pathlib").Path("/no/such/config"))
    assert fw == {"local": [], "remote": [], "dynamic": []}
