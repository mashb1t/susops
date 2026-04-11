"""PAC file generation and Python HTTP server for SusOps."""
from __future__ import annotations
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from susops.core.config import SusOpsConfig

from susops.core.ports import cidr_to_netmask

__all__ = ["generate_pac", "write_pac_file", "PacServer"]

_DIRECT = '"DIRECT"'


def _is_wildcard(host: str) -> bool:
    return "*" in host or "?" in host


def _is_cidr(host: str) -> bool:
    return re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', host) is not None


def _pac_rule(host: str, socks_port: int) -> str:
    """Generate a single PAC rule line for a host/CIDR/wildcard."""
    proxy = f"SOCKS5 127.0.0.1:{socks_port}"
    if _is_wildcard(host):
        return f"  if (shExpMatch(host, '{host}')) return '{proxy}';"
    if _is_cidr(host):
        net, bits = host.split("/")
        mask = cidr_to_netmask(int(bits))
        return f"  if (isInNet(host, '{net}', '{mask}')) return '{proxy}';"
    # Plain hostname
    return f"  if (host == '{host}' || dnsDomainIs(host, '.{host}')) return '{proxy}';"


def generate_pac(config: "SusOpsConfig", active_tags: set[str] | None = None) -> str:
    """Generate the FindProxyForURL JavaScript PAC function.

    When active_tags is provided, only connections in that set are included.
    When None, includes all connections (legacy behavior).
    """
    lines = ["function FindProxyForURL(url, host) {"]

    for conn in config.connections:
        if conn.socks_proxy_port == 0:
            continue
        if active_tags is not None and conn.tag not in active_tags:
            continue
        for host in conn.pac_hosts:
            lines.append(_pac_rule(host, conn.socks_proxy_port))

    lines.append(f'  return {_DIRECT};')
    lines.append("}")
    return "\n".join(lines)


def write_pac_file(config: "SusOpsConfig", workspace: Path, active_tags: set[str] | None = None) -> Path:
    """Write the PAC file to <workspace>/susops.pac and return its path."""
    pac_path = workspace / "susops.pac"
    pac_content = generate_pac(config, active_tags=active_tags)
    pac_path.write_text(pac_content)
    return pac_path


class _PacHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves the PAC file."""

    pac_path: Path  # set by PacServer before creating HTTPServer

    def do_GET(self) -> None:
        if self.path not in ("/susops.pac", "/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            content = self.server.pac_path.read_bytes()  # type: ignore[attr-defined]
        except OSError:
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        """POST /stop — remote shutdown so other processes can stop this server."""
        if self.path != "/stop":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        # Shut down in a background thread so the response can be sent first
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # suppress default access log


class PacServer:
    """Python HTTP server that serves the PAC file.

    Runs in a daemon thread. Replaces the nc-based loop in the original bash CLI.
    """

    def __init__(self) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0
        self._pac_path: Path | None = None

    def start(self, port: int, pac_path: Path) -> None:
        """Start the PAC HTTP server on the given port.

        Raises RuntimeError if already running or if port is in use.
        """
        if self._server is not None:
            raise RuntimeError("PAC server is already running")

        self._pac_path = pac_path

        # Create HTTPServer and attach pac_path so handler can access it
        server = HTTPServer(("127.0.0.1", port), _PacHandler)
        server.pac_path = pac_path  # type: ignore[attr-defined]

        self._server = server
        self._port = server.server_address[1]  # actual port (in case 0 was given)

        self._thread = threading.Thread(
            target=server.serve_forever,
            name="susops-pac-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the PAC HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._port = 0

    def is_running(self) -> bool:
        """Return True if the server is currently running."""
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def get_port(self) -> int:
        """Return the port the server is listening on (0 if not running)."""
        return self._port

    def get_pac_path(self) -> Path | None:
        """Return the PAC file path currently being served."""
        return self._pac_path

    def reload(self, pac_path: Path) -> None:
        """Update the PAC file path (takes effect on next request)."""
        if self._server is not None:
            self._server.pac_path = pac_path  # type: ignore[attr-defined]
        self._pac_path = pac_path


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="SusOps PAC server (background)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--pac-file", type=Path, required=True)
    args = parser.parse_args()

    _server = HTTPServer(("127.0.0.1", args.port), _PacHandler)
    _server.pac_path = args.pac_file  # type: ignore[attr-defined]
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _server.server_close()
    sys.exit(0)
