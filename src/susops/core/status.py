"""SSE StatusServer — real-time event push for TUI and tray apps.

Provides a Server-Sent Events endpoint at GET /events that broadcasts
state, share, bandwidth, and forward events to all connected clients.

Requires: aiohttp>=3.9 (install with pip install susops[share])

Event types:
  state     — {"tag": "work", "running": true, "pid": 1234}
  share     — {"port": 52100, "file": "report.pdf", "running": true, "conn_tag": "work"}
  bandwidth — {"tag": "work", "rx_bps": 1234.5, "tx_bps": 567.8}
  forward   — {"tag": "work", "fw_tag": "db", "direction": "local", "running": true}
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

__all__ = ["StatusServer"]


class StatusServer:
    """Async SSE server that broadcasts events to connected clients.

    Reuses the shared event loop from share.py so both servers run on the
    same daemon thread.
    """

    def __init__(self) -> None:
        self._runner = None
        self._port: int = 0
        self._queues: list[asyncio.Queue[str | None]] = []
        self._queues_lock: asyncio.Lock | None = None

    def start(self, port: int = 0) -> int:
        """Start the SSE server. Returns the bound port."""
        from susops.core.share import _get_loop

        if self._runner is not None:
            return self._port

        loop = _get_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._start_async(port), loop
        )
        self._port = future.result(timeout=10)
        return self._port

    async def _start_async(self, port: int) -> int:
        from aiohttp import web

        self._queues_lock = asyncio.Lock()

        async def handle_events(request: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Access-Control-Allow-Origin": "*",
                },
            )
            await resp.prepare(request)

            queue: asyncio.Queue[str | None] = asyncio.Queue()
            async with self._queues_lock:
                self._queues.append(queue)

            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=2.0)
                    except asyncio.TimeoutError:
                        # Send a keepalive comment so the client socket stays
                        # alive and doesn't hit its read timeout.
                        await resp.write(b": ping\n\n")
                        continue
                    if msg is None:
                        break
                    await resp.write(msg.encode())
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            finally:
                async with self._queues_lock:
                    self._queues.remove(queue)

            return resp

        app = web.Application()
        app.router.add_get("/events", handle_events)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        self._runner = runner
        return site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    def stop(self) -> None:
        """Stop the SSE server and disconnect all clients."""
        if self._runner is None:
            return

        from susops.core.share import _get_loop

        loop = _get_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._stop_async(), loop
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._runner = None
        self._port = 0

    async def _stop_async(self) -> None:
        # Signal all clients to disconnect
        if self._queues_lock is not None:
            async with self._queues_lock:
                for q in self._queues:
                    await q.put(None)
        if self._runner is not None:
            await self._runner.cleanup()

    def emit(self, event: str, data: dict[str, Any]) -> None:
        """Broadcast an SSE event to all connected clients (thread-safe)."""
        if self._runner is None:
            return

        from susops.core.share import _get_loop

        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        loop = _get_loop()
        asyncio.run_coroutine_threadsafe(
            self._broadcast(payload), loop
        )

    async def _broadcast(self, payload: str) -> None:
        if self._queues_lock is None:
            return
        async with self._queues_lock:
            for q in list(self._queues):
                await q.put(payload)

    def is_running(self) -> bool:
        return self._runner is not None

    def get_port(self) -> int:
        return self._port
