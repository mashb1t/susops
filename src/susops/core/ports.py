"""Port allocation, validation, and CIDR utilities for SusOps."""
from __future__ import annotations
import random
import socket
import struct

__all__ = [
    "get_random_free_port",
    "is_port_free",
    "validate_port",
    "cidr_to_netmask",
    "check_local_port_conflict",
]

def get_random_free_port(start: int = 49152, end: int = 65535) -> int:
    """Return a random free TCP port in [start, end].

    Uses socket.bind to test availability — no lsof required.
    Raises RuntimeError if no free port found after 100 attempts.
    """
    for _ in range(100):
        port = random.randint(start, end)
        if is_port_free(port):
            return port
    raise RuntimeError(f"No free port found in range {start}-{end}")

def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if the given TCP port is not currently bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False

def validate_port(port: int) -> bool:
    """Return True if port is in valid range 1–65535."""
    return isinstance(port, int) and 1 <= port <= 65535

def cidr_to_netmask(cidr_bits: int) -> str:
    """Convert CIDR prefix length to dotted-decimal netmask.

    Example: cidr_to_netmask(24) -> "255.255.255.0"
    """
    if not 0 <= cidr_bits <= 32:
        raise ValueError(f"CIDR bits must be 0-32, got {cidr_bits}")
    mask = (0xFFFFFFFF << (32 - cidr_bits)) & 0xFFFFFFFF
    return socket.inet_ntoa(struct.pack(">I", mask))

def check_local_port_conflict(port: int) -> str | None:
    """Return an error message if port conflicts, or None if it's free.

    Checks: valid range, port is free on localhost.
    """
    if not validate_port(port):
        return f"Port {port} is out of valid range (1-65535)"
    if not is_port_free(port):
        return f"Port {port} is already in use on localhost"
    return None
