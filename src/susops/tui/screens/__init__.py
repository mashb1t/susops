"""Shared helpers for TUI screens."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import susops
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static


def open_in_explorer(file_path: str) -> None:
    """Open the parent directory of file_path in the system file manager."""
    parent = str(Path(file_path).parent)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", file_path])
    else:
        subprocess.Popen(["xdg-open", parent])


def share_name_markup(file_path: str, name: str) -> str:
    """Return Rich action-link markup for a share filename, or plain name if path contains a single quote."""
    if "'" not in file_path:
        return f"[@click=screen.open_share('{file_path}')]{name}[/]"
    return name


def compose_footer() -> ComposeResult:
    """Yield a footer row with key bindings and the app version on the right."""
    with Horizontal(classes="footer-row"):
        yield Footer()
        yield Static("[dim]S[/dim]usOps", classes="footer-logo", markup=True)
        yield Static(f"v{susops.__version__}", classes="footer-version")
