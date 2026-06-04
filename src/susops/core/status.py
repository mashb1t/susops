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
import time
from typing import Any, Callable

__all__ = ["StatusServer"]


class StatusServer:
    """Async SSE server that broadcasts events to connected clients.

    Reuses the shared event loop from shares.py so both servers run on the
    same daemon thread.
    """

    def __init__(self) -> None:
        self._runner = None
        self._port: int = 0
        self._queues: list[asyncio.Queue[str | None]] = []
        self._queues_lock: asyncio.Lock | None = None
        # Set by the services daemon to react to client-count changes — used
        # to drive idle-shutdown.
        self.on_clients_changed: Callable[[int], None] | None = None
        # Set by the services daemon (wired to mgr._log) to record connect /
        # disconnect events in the in-memory log buffer.
        self.on_log: Callable[[str], None] | None = None

    def client_count(self) -> int:
        return len(self._queues)

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

        # SSE comment lines (lines starting with ":") are valid no-ops per the
        # spec; we use them as a periodic keepalive so a write actually
        # happens even when the daemon has no events to broadcast. Without
        # this, a frontend that closes its TCP connection is never noticed —
        # `queue.get()` blocks forever and `_fire_clients_changed(0)` never
        # fires, which means idle-shutdown can't trigger.
        HEARTBEAT_INTERVAL_S = 5.0

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

            # Identify the client. Frontends set `X-Susops-Client` to one of
            # tui / tray-mac / tray-linux / cli; anything else is "other".
            client_type = request.headers.get("X-Susops-Client", "other")
            client_version = request.headers.get("X-Susops-Client-Version", "?")
            client_pid = request.headers.get("X-Susops-Pid", "?")
            # Optional per-client event filter: comma-separated list of event
            # names (e.g. "state,share"). When set, the broadcaster only
            # pushes matching events to this client — useful for clients
            # like the tray that don't render bandwidth charts.
            events_header = request.headers.get("X-Susops-Events", "").strip()
            event_filter: set[str] | None = (
                {e.strip() for e in events_header.split(",") if e.strip()}
                if events_header else None
            )
            peer = request.transport.get_extra_info("peername") if request.transport else None
            peer_str = f"{peer[0]}:{peer[1]}" if peer else "?"
            connect_t = time.monotonic()

            queue: asyncio.Queue[str | None] = asyncio.Queue()
            queue._susops_events = event_filter  # type: ignore[attr-defined]
            async with self._queues_lock:
                self._queues.append(queue)
                count = len(self._queues)
            self._fire_clients_changed(count)
            # Always include the subscribed event set in the connect line —
            # "all" when the client sent no X-Susops-Events filter, the
            # explicit list otherwise. Useful to confirm what each client
            # actually subscribed to (e.g. tray opts out of bandwidth).
            events_str = (
                ",".join(sorted(event_filter))
                if event_filter is not None else "all"
            )
            self._fire_log(
                f"SSE client connected: {client_type} v{client_version} "
                f"(pid {client_pid}, peer {peer_str}, events: {events_str}) — {count} active"
            )

            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(
                            queue.get(), timeout=HEARTBEAT_INTERVAL_S,
                        )
                    except asyncio.TimeoutError:
                        # No event in HEARTBEAT_INTERVAL_S — send a comment
                        # line. If the client is gone the write will raise
                        # ConnectionResetError and we'll fall into the
                        # finally block, decrementing the client count.
                        await resp.write(b": keepalive\n\n")
                        continue
                    if msg is None:
                        break
                    await resp.write(msg.encode())
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            except Exception:
                # Any other write failure also implies the client is gone.
                pass
            finally:
                async with self._queues_lock:
                    try:
                        self._queues.remove(queue)
                    except ValueError:
                        pass
                    count = len(self._queues)
                self._fire_clients_changed(count)
                duration = time.monotonic() - connect_t
                self._fire_log(
                    f"SSE client disconnected: {client_type} "
                    f"(pid {client_pid}, after {duration:.1f}s) — {count} active"
                )

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
        """Broadcast an SSE event to all connected clients (thread-safe).

        Honours each client's per-connection event filter (set via the
        ``X-Susops-Events`` header). Clients with no filter receive every
        event; clients with a filter only receive events whose name is in
        their set.
        """
        if self._runner is None:
            return

        from susops.core.share import _get_loop

        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        loop = _get_loop()
        asyncio.run_coroutine_threadsafe(
            self._broadcast(event, payload), loop
        )

    async def _broadcast(self, event: str, payload: str) -> None:
        if self._queues_lock is None:
            return
        async with self._queues_lock:
            for q in list(self._queues):
                allowed: set[str] | None = getattr(q, "_susops_events", None)
                if allowed is not None and event not in allowed:
                    continue
                await q.put(payload)

    def is_running(self) -> bool:
        return self._runner is not None

    def get_port(self) -> int:
        return self._port

    def _fire_clients_changed(self, count: int) -> None:
        cb = self.on_clients_changed
        if cb is None:
            return
        try:
            cb(count)
        except Exception:
            pass

    def _fire_log(self, msg: str) -> None:
        cb = self.on_log
        if cb is None:
            return
        try:
            cb(msg)
        except Exception:
            pass
