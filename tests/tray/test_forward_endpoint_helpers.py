"""Tests for the shared tray forward-endpoint helpers (used by both trays)."""
from __future__ import annotations

from types import SimpleNamespace

from susops.tray.base import (
    BIND_PRESETS,
    existing_forward_binds,
    resolve_forward_endpoint,
)


def test_resolve_endpoint_port():
    ep, err = resolve_forward_endpoint("5432", "", "Source")
    assert err is None
    assert ep == (5432, "")


def test_resolve_endpoint_socket():
    ep, err = resolve_forward_endpoint("", "/var/run/docker.sock", "Destination")
    assert err is None
    assert ep == (0, "/var/run/docker.sock")


def test_resolve_endpoint_both_rejected():
    ep, err = resolve_forward_endpoint("5432", "/tmp/x.sock", "Source")
    assert ep is None
    assert err[0] == "Invalid Forward" and "not both" in err[1]


def test_resolve_endpoint_neither_rejected():
    ep, err = resolve_forward_endpoint("", "", "Source")
    assert ep is None
    assert "required" in err[1]


def test_resolve_endpoint_bad_port():
    ep, err = resolve_forward_endpoint("70000", "", "Source")
    assert ep is None
    assert err[0] == "Invalid Source Port"


def test_resolve_endpoint_bad_socket_path():
    ep, err = resolve_forward_endpoint("", "relative.sock", "Source")
    assert ep is None
    assert err[0] == "Invalid Socket"


def _fw(**kw):
    base = dict(src_addr="localhost", src_port=0, src_socket="",
               dst_addr="localhost", dst_port=0, dst_socket="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_existing_forward_binds_dedup_and_skips_presets():
    conns = [
        SimpleNamespace(forwards=SimpleNamespace(
            local=[_fw(src_addr="172.120.0.1", src_port=1),
                   _fw(src_addr="localhost", src_port=2)],  # preset skipped
            remote=[_fw(src_addr="172.120.0.1", src_port=3,  # dup
                        dst_addr="10.0.0.5", dst_port=4)],
        )),
    ]
    cfg = SimpleNamespace(connections=conns)
    binds = existing_forward_binds(cfg)
    assert "172.120.0.1" in binds and "10.0.0.5" in binds
    assert "localhost" not in binds          # preset excluded
    assert binds.count("172.120.0.1") == 1   # de-duplicated
    assert all(b not in BIND_PRESETS for b in binds)
