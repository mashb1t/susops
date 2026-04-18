"""Shared helpers for TUI screens."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import susops
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Label, Static

from susops.core.config import PortForward as _PortForward


class _CollapsingLabel(Label):
    """Label that is display:none with zero margin/padding when empty, visible when it has content."""

    def on_mount(self) -> None:
        self.styles.margin = 0
        self.styles.padding = 0
        self.display = False

    def update(self, renderable="") -> None:
        super().update(renderable)
        self.display = bool(str(renderable).strip())


def open_path(path: str) -> None:
    """Open a file or directory with the system default handler."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_in_explorer(file_path: str) -> None:
    """Open the parent directory of file_path in the system file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", file_path])
    else:
        subprocess.Popen(["xdg-open", str(Path(file_path).parent)])


def share_name_markup(file_path: str, name: str) -> str:
    """Return Rich action-link markup for a share filename, or plain name if path contains a single quote."""
    if "'" not in file_path:
        return f"[@click=screen.open_share('{file_path}')]{name}[/]"
    return name


def compose_footer() -> ComposeResult:
    """Yield a footer row with key bindings and the app version on the right."""
    with Horizontal(classes="footer-row"):
        yield Footer()
        yield Static(f"[@click=app.open_github()]v{susops.__version__}[/]", classes="footer-version", markup=True)


def proto_label(fw: _PortForward) -> str:
    """Return display string for a forward's protocol(s): TCP, UDP, or TCP+UDP."""
    if fw.tcp and fw.udp:
        return "TCP+UDP"
    if fw.udp:
        return "UDP"
    return "TCP"


def status_dot(running: bool, enabled: bool = True, partial: bool = False) -> str:
    """Return Rich markup for the status indicator dot.

    partial=True (TCP+UDP where one protocol is down) → yellow ●
    running=True → green ●
    enabled=True, not running → red ○
    enabled=False → ─ (disabled)
    """
    if not enabled:
        return "─"
    if partial:
        return "[yellow]●[/yellow]"
    if running:
        return "[green]●[/green]"
    return "[red]○[/red]"


def share_status_dot(running: bool, stopped: bool) -> str:
    """Return Rich markup for a share status dot.

    running=True → green ●
    stopped=True (manually stopped) → ─
    otherwise (connection down) → red ○
    """
    if running:
        return "[green]●[/green]"
    if stopped:
        return "─"
    return "[red]○[/red]"


def fmt_bps(bps: float) -> str:
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f}MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f}kB/s"
    return f"{bps:.0f}B/s"


def fmt_bytes(b: float) -> str:
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f}GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f}MB"
    if b >= 1024:
        return f"{b / 1024:.0f}kB"
    return f"{b:.0f}B"
