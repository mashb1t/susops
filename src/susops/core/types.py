"""Types module for SusOps.

Defines all shared enums and result dataclasses used throughout the susops package.
This module is the single source of truth for type definitions — no other module
should define these types.

Enums:
  - ProcessState: Enumeration of SSH/PAC process states
  - LogoStyle: Enumeration of logo style options

Result Dataclasses:
  - ConnectionStatus: Status of a single SSH connection
  - StartResult: Result of starting connections/PAC
  - StopResult: Result of stopping connections/PAC
  - StatusResult: Overall status query result
  - TestResult: Result of testing a connection
  - ShareInfo: Information about an active file share
"""

import dataclasses
import enum
from typing import Optional


__all__ = [
    "ProcessState",
    "LogoStyle",
    "ConnectionStatus",
    "StartResult",
    "StopResult",
    "StatusResult",
    "TestResult",
    "ShareInfo",
]


class ProcessState(enum.Enum):
    """Enumeration of possible process states for SSH and PAC services."""

    INITIAL = "initial"
    """Not yet checked — initial state before status inquiry."""

    RUNNING = "running"
    """All services are up and running."""

    STOPPED_PARTIALLY = "stopped_partially"
    """Some services are down, others are running."""

    STOPPED = "stopped"
    """All services are down."""

    ERROR = "error"
    """Unexpected state or error occurred."""


class LogoStyle(enum.Enum):
    """Enumeration of logo style options for UI display."""

    COLORED_GLASSES = "COLORED_GLASSES"
    """Colored glasses logo variant."""

    COLORED_S = "COLORED_S"
    """Colored S logo variant."""

    GEAR = "GEAR"
    """Gear logo variant."""


@dataclasses.dataclass(frozen=True)
class ConnectionStatus:
    """Status information for a single SSH connection.

    Attributes:
        tag: Unique identifier for the connection (e.g., 'proxy1', 'vpn')
        running: Whether the connection is currently active
        pid: Process ID of the autossh/ssh process, or None if not running
        socks_port: Port number on which the SOCKS proxy is listening (0 if not running)
    """

    tag: str
    running: bool
    pid: Optional[int] = None
    socks_port: int = 0


@dataclasses.dataclass(frozen=True)
class StartResult:
    """Result of attempting to start connections and/or PAC service.

    Attributes:
        success: Whether the operation succeeded
        message: Human-readable status message
        connection_statuses: Tuple of ConnectionStatus objects for each connection
    """

    success: bool
    message: str
    connection_statuses: tuple[ConnectionStatus, ...] = ()


@dataclasses.dataclass(frozen=True)
class StopResult:
    """Result of attempting to stop connections and/or PAC service.

    Attributes:
        success: Whether the operation succeeded
        message: Human-readable status message
    """

    success: bool
    message: str


@dataclasses.dataclass(frozen=True)
class StatusResult:
    """Result of querying overall status of connections and services.

    Attributes:
        state: The overall ProcessState
        connection_statuses: Tuple of ConnectionStatus objects for each connection
        pac_running: Whether the PAC (Proxy Auto-Config) service is running
        pac_port: Port number on which PAC HTTP server is listening
        message: Optional human-readable status message
    """

    state: ProcessState
    connection_statuses: tuple[ConnectionStatus, ...]
    pac_running: bool
    pac_port: int
    message: str = ""


@dataclasses.dataclass(frozen=True)
class TestResult:
    """Result of testing connectivity through a connection.

    Attributes:
        target: Description of the target that was tested (e.g., hostname, URL)
        success: Whether the test succeeded
        message: Human-readable result or error message
        latency_ms: Measured round-trip latency in milliseconds, or None if test failed
    """

    target: str
    success: bool
    message: str
    latency_ms: Optional[float] = None


@dataclasses.dataclass(frozen=True)
class ShareInfo:
    """Information about an active file share session.

    Attributes:
        file_path: Absolute path to the file being shared
        port: Port number on which the file server is listening
        password: Authentication password for accessing the share
        url: Full HTTP URL for accessing the share (e.g., 'http://localhost:8080')
        conn_tag: Connection tag used for port forwarding, or None for local-only share
        running: Whether the share server is currently active
    """

    file_path: str
    port: int
    password: str
    url: str
    conn_tag: str | None = None
    running: bool = True
