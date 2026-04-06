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


def generate_pac(config: "SusOpsConfig") -> str:
    """Generate the FindProxyForURL JavaScript PAC function.

    Includes rules for ALL connections (not just the active one),
    matching the behavior of the original bash implementation.
    """
    lines = ["function FindProxyForURL(url, host) {"]

    for conn in config.connections:
        if conn.socks_proxy_port == 0:
            continue  # skip connections without an assigned port
        for host in conn.pac_hosts:
            lines.append(_pac_rule(host, conn.socks_proxy_port))

    lines.append(f'  return {_DIRECT};')
    lines.append("}")
    return "\n".join(lines)


def write_pac_file(config: "SusOpsConfig", workspace: Path) -> Path:
    """Write the PAC file to <workspace>/susops.pac and return its path."""
    pac_path = workspace / "susops.pac"
    pac_content = generate_pac(config)
    pac_path.write_text(pac_content)
    return pac_path


class _PacHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves the PAC file."""

    pac_path: Path  # set by PacServer before creating HTTPServer

    def do_GET(self) -> None:  # noqa: N802
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

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
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

    def reload(self, pac_path: Path) -> None:
        """Update the PAC file path (takes effect on next request)."""
        if self._server is not None:
            self._server.pac_path = pac_path  # type: ignore[attr-defined]
        self._pac_path = pac_path
