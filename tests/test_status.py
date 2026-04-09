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
