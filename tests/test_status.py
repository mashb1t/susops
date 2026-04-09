"""Tests for susops.core.status — SSE StatusServer."""
from __future__ import annotations

import time

pytest_plugins = []

try:
    import aiohttp  # noqa: F401
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

import pytest

pytestmark = pytest.mark.skipif(
    not _HAS_AIOHTTP, reason="aiohttp required for StatusServer tests"
)


@pytest.fixture
def status_server():
    from susops.core.status import StatusServer
    srv = StatusServer()
    port = srv.start(port=0)
    yield srv, port
    srv.stop()


def test_status_server_starts(status_server):
    srv, port = status_server
    assert srv.is_running()
    assert port > 0


def test_status_server_stop(status_server):
    srv, _port = status_server
    srv.stop()
    assert not srv.is_running()


def test_emit_reaches_client(status_server):
    """Connect via urllib and verify an emitted event arrives."""
    import threading
    import urllib.request

    srv, port = status_server
    received: list[str] = []
    done = threading.Event()

    def _listen():
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/events")
            with urllib.request.urlopen(req, timeout=5) as resp:
                for raw in resp:
                    line = raw.decode()
                    received.append(line)
                    # Wait until we have both the event line and the data line
                    if (
                        any("event: test" in r for r in received)
                        and any("data:" in r for r in received)
                    ):
                        done.set()
                        return
        except Exception:
            done.set()

    t = threading.Thread(target=_listen, daemon=True)
    t.start()

    time.sleep(0.1)  # let client connect
    srv.emit("test", {"hello": "world"})
    done.wait(timeout=3.0)

    assert any("event: test" in r for r in received)
    assert any("hello" in r for r in received)


def test_emit_before_any_client_is_noop(status_server):
    """emit() with no connected clients must not raise."""
    srv, _port = status_server
    # Should not raise even though nobody is listening
    srv.emit("state", {"tag": "work", "running": False, "pid": None})


def test_emit_multiple_clients_both_receive(status_server):
    """All connected clients receive each emitted event."""
    import threading
    import urllib.request

    srv, port = status_server
    results: dict[int, list[str]] = {0: [], 1: []}
    dones = [threading.Event(), threading.Event()]

    def _listen(idx: int) -> None:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/events")
            with urllib.request.urlopen(req, timeout=5) as resp:
                for raw in resp:
                    line = raw.decode()
                    results[idx].append(line)
                    if (
                        any("event: multi" in r for r in results[idx])
                        and any("data:" in r for r in results[idx])
                    ):
                        dones[idx].set()
                        return
        except Exception:
            dones[idx].set()

    threads = [threading.Thread(target=_listen, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()

    time.sleep(0.15)  # let both clients connect
    srv.emit("multi", {"value": 42})

    for done in dones:
        done.wait(timeout=3.0)

    for idx in range(2):
        assert any("event: multi" in r for r in results[idx]), f"Client {idx} missed the event"
        assert any("42" in r for r in results[idx]), f"Client {idx} missed the data"
