"""Tests for the Linux tray's _read_endpoint (Port/Socket stack read logic).

The GTK dialogs can't run headless, but _read_endpoint is a plain function
operating on a widget dict — exercise it with fakes to lock in the port-vs-
socket read + error propagation the toggle relies on.
"""
from __future__ import annotations

from susops.tray.linux import _read_endpoint


class _Entry:
    def __init__(self, text=""):
        self._t = text

    def get_text(self):
        return self._t


class _Combo:
    def __init__(self, text="localhost"):
        self._e = _Entry(text)

    def get_child(self):
        return self._e


class _Stack:
    def __init__(self, name):
        self._n = name

    def get_visible_child_name(self):
        return self._n


def _ep(mode, *, port="", socket="", addr="localhost"):
    return {"stack": _Stack(mode), "port": _Entry(port),
            "socket": _Entry(socket), "addr": _Combo(addr)}


def test_read_endpoint_port_mode():
    res, err = _read_endpoint(_ep("port", port="5432", addr="0.0.0.0"), "Source")
    assert err is None
    assert res == (5432, "", "0.0.0.0")


def test_read_endpoint_socket_mode():
    res, err = _read_endpoint(
        _ep("socket", socket="/var/run/docker.sock"), "Destination")
    assert err is None
    assert res == (0, "/var/run/docker.sock", "localhost")


def test_read_endpoint_socket_mode_ignores_port_field():
    # In socket mode the port field is not read even if it has stale text.
    res, err = _read_endpoint(
        _ep("socket", port="9999", socket="/tmp/x.sock"), "Source")
    assert err is None
    assert res == (0, "/tmp/x.sock", "localhost")


def test_read_endpoint_bad_port():
    res, err = _read_endpoint(_ep("port", port="70000"), "Source")
    assert res is None
    assert err[0] == "Invalid Source Port"


def test_read_endpoint_bad_socket_path():
    res, err = _read_endpoint(_ep("socket", socket="relative.sock"), "Source")
    assert res is None
    assert err[0] == "Invalid Socket"
