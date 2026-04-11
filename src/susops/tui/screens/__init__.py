"""Shared helpers for TUI screens."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import susops
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static


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


from susops.core.config import PortForward as _PortForward


def proto_label(fw: _PortForward) -> str:
    """Return display string for a forward's protocol(s): TCP, UDP, or TCP+UDP."""
    if fw.tcp and fw.udp:
        return "TCP+UDP"
    if fw.udp:
        return "UDP"
    return "TCP"
