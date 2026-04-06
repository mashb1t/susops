# SusOps

SSH SOCKS5 proxy manager with PAC server, Textual TUI, and system tray apps.

## Overview

SusOps is a unified tool for managing SSH SOCKS5 proxy tunnels with an integrated PAC (Proxy Auto-Config) server. It provides:

- A Textual-based TUI for terminal users
- System tray apps for Linux (GTK3) and macOS (rumps)
- A programmatic Python API

## Installation

```bash
pip install susops[tui]          # TUI only
pip install susops[tui,tray-linux]  # TUI + Linux tray
pip install susops[tui,tray-mac]    # TUI + macOS tray
```

## Usage

```bash
susops   # or: so
```

## Development

```bash
pip install -e ".[dev,tui]"
pytest
```
