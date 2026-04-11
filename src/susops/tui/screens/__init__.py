"""Shared helpers for TUI screens."""
from __future__ import annotations

import susops
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Static


def compose_footer() -> ComposeResult:
    """Yield a footer row with key bindings and the app version on the right."""
    with Horizontal(classes="footer-row"):
        yield Footer()
        yield Static("[dim]S[/dim]usOps", classes="footer-logo", markup=True)
        yield Static(f"v{susops.__version__}", classes="footer-version")
