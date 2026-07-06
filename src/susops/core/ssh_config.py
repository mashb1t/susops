"""SSH config parser for hostname autocompletion and forward import."""
from __future__ import annotations

import re
from pathlib import Path

__all__ = ["get_ssh_hosts", "parse_ssh_forwards"]

_SSH_CONFIG = Path.home() / ".ssh" / "config"


def get_ssh_hosts(ssh_config_path: Path | None = None) -> list[str]:
    """Parse ~/.ssh/config and return all non-wildcard Host entries.

    Returns a sorted list of hostnames suitable for autocomplete.
    Skips wildcard entries like '*' or '*.example.com'.
    """
    if ssh_config_path is None:
        ssh_config_path = _SSH_CONFIG
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


def _split_bind_port(spec: str) -> tuple[str, int] | None:
    """Parse "[bind:]port" -> (bind, port). Default bind localhost."""
    if ":" in spec:
        bind, _, port = spec.rpartition(":")
        bind = bind or "localhost"
    else:
        bind, port = "localhost", spec
    try:
        return bind, int(port)
    except ValueError:
        return None


def parse_ssh_forwards(host: str, ssh_config_path: Path | None = None) -> dict:
    """Return the LocalForward / RemoteForward / DynamicForward directives that
    apply to `host` in ~/.ssh/config.

    Result: {"local": [fw...], "remote": [fw...], "dynamic": [port...]} where
    each fw is a dict {src_addr, src_port, dst_addr, dst_port}. Matching is
    literal on the Host token(s) (wildcard blocks are ignored for import). This
    lets a user adopt forwards already defined in their ssh config in one step.
    """
    out: dict = {"local": [], "remote": [], "dynamic": []}
    if ssh_config_path is None:
        ssh_config_path = _SSH_CONFIG
    if not ssh_config_path.exists():
        return out
    try:
        content = ssh_config_path.read_text(errors="replace")
    except OSError:
        return out

    in_block = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^[Hh]ost\s+(.+)$', line)
        if m:
            in_block = host in m.group(1).split()
            continue
        if not in_block:
            continue
        parts = line.split()
        key = parts[0].lower()
        if key == "localforward" and len(parts) >= 3:
            src = _split_bind_port(parts[1])
            dst = parts[2].rpartition(":")
            if src and dst[1]:
                try:
                    out["local"].append({
                        "src_addr": src[0], "src_port": src[1],
                        "dst_addr": dst[0], "dst_port": int(dst[2]),
                    })
                except ValueError:
                    pass
        elif key == "remoteforward" and len(parts) >= 3:
            src = _split_bind_port(parts[1])
            dst = parts[2].rpartition(":")
            if src and dst[1]:
                try:
                    out["remote"].append({
                        "src_addr": src[0], "src_port": src[1],
                        "dst_addr": dst[0], "dst_port": int(dst[2]),
                    })
                except ValueError:
                    pass
        elif key == "dynamicforward" and len(parts) >= 2:
            bp = _split_bind_port(parts[1])
            if bp:
                out["dynamic"].append(bp[1])
    return out
