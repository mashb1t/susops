import json
import socket

import pytest

from susops.tray.debug_server import TrayDebugServer


@pytest.fixture
def server():
    handlers = {
        "echo": lambda args: {"args": args},
        "boom": lambda args: (_ for _ in ()).throw(RuntimeError("kaput")),
        "none": lambda args: None,
    }
    srv = TrayDebugServer(handlers, port=0)
    srv.start()
    yield srv
    srv.close()


def _send(port: int, line: str) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        f = s.makefile("rw", encoding="utf-8")
        f.write(line + "\n")
        f.flush()
        return json.loads(f.readline())


def test_dispatches_to_handler(server):
    assert _send(server.port, "echo a b") == {"args": ["a", "b"]}


def test_unknown_command_is_error(server):
    assert "error" in _send(server.port, "nope")


def test_handler_exception_is_error_not_crash(server):
    assert _send(server.port, "boom") == {"error": "kaput"}
    # server still alive afterwards
    assert _send(server.port, "echo x") == {"args": ["x"]}


def test_none_result_means_ok(server):
    assert _send(server.port, "none") == {"ok": True}


def test_binds_localhost_only(server):
    assert server._sock.getsockname()[0] == "127.0.0.1"
