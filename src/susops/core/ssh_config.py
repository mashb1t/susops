"""SSH config parser for hostname autocompletion."""
from __future__ import annotations
from pathlib import Path
import re

__all__ = ["get_ssh_hosts"]

_SSH_CONFIG = Path.home() / ".ssh" / "config"

def get_ssh_hosts(ssh_config_path: Path = _SSH_CONFIG) -> list[str]:
    """Parse ~/.ssh/config and return all non-wildcard Host entries.

    Returns a sorted list of hostnames suitable for autocomplete.
    Skips wildcard entries like '*' or '*.example.com'.
    """
    if not ssh_config_path.exists():
        return []

    hosts: list[str] = []
    try:
        content = ssh_config_path.read_text(errors="replace")
    except OSError:
        return []

    for line in content.splitlines():
        line = line.strip()
        # Match "Host <name>" lines (case-insensitive)
        match = re.match(r'^[Hh]ost\s+(.+)$', line)
        if not match:
            continue
        # A Host line can have multiple space-separated entries
        for entry in match.group(1).split():
            # Skip wildcards
            if '*' in entry or '?' in entry:
                continue
            hosts.append(entry)

    return sorted(set(hosts))
