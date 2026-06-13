"""Opt-in localhost TCP command server for driving a tray app in tests/dev loops.

Platform-neutral: knows nothing about AppKit/GTK. Handlers receive the
argument list and return a JSON-able dict (None means {"ok": true}). UI-touching
handlers are responsible for marshaling onto their toolkit's main thread.

Protocol: newline-delimited commands ("cmd arg1 arg2"), one JSON object per
line in response. Only ever bound to 127.0.0.1; only started when the tray
is launched with SUSOPS_TRAY_DEBUG_PORT set.
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Callable

Handler = Callable[[list[str]], dict | None]


class TrayDebugServer:
    def __init__(self, handlers: dict[str, Handler], port: int = 0) -> None:
        self._handlers = handlers
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="susops-tray-debug",
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True,
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            f = conn.makefile("rw", encoding="utf-8")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                f.write(json.dumps(self._dispatch(line)) + "\n")
                f.flush()

    def _dispatch(self, line: str) -> dict:
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        handler = self._handlers.get(cmd)
        if handler is None:
            known = ", ".join(sorted(self._handlers))
            return {"error": f"unknown command: {cmd} (known: {known})"}
        try:
            return handler(args) or {"ok": True}
        except Exception as exc:
            return {"error": str(exc)}
