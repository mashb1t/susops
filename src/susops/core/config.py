"""Config module for SusOps.

Defines all Pydantic v2 config models and handles reading/writing
~/.susops/config.yaml using ruamel.yaml for comment preservation.

Models:
  - PortForward: A single port forward rule (local or remote)
  - Forwards: Container for local and remote port forward lists
  - Connection: A single SSH connection configuration
  - AppConfig: Application-level settings
  - SusOpsConfig: Root config model

I/O Functions:
  - get_config_path: Resolve path to config.yaml
  - load_config: Load config from disk, creating defaults if missing
  - save_config: Persist config to disk with ruamel.yaml

Helper Functions:
  - get_connection: Find a connection by tag
  - get_default_connection: Return the first connection or None
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from ruamel.yaml import YAML

from susops.core.types import LogoStyle


__all__ = [
    "PortForward",
    "Forwards",
    "FileShare",
    "Connection",
    "AppConfig",
    "SusOpsConfig",
    "get_config_path",
    "load_config",
    "save_config",
    "get_connection",
    "get_default_connection",
    "WORKSPACE_DEFAULT",
    "CONFIG_FILENAME",
]

WORKSPACE_DEFAULT = Path.home() / ".susops"
CONFIG_FILENAME = "config.yaml"


class PortForward(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tag: str = ""
    src_addr: str = "localhost"
    src_port: int
    dst_addr: str = "localhost"
    dst_port: int
    tcp: bool = True
    udp: bool = False
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def handle_legacy_schema(cls, data: Any) -> Any:
        """Handle old schema where 'src'/'dst' were plain port numbers."""
        if isinstance(data, dict) and "src" in data and "src_port" not in data:
            data = dict(data)
            data["src_port"] = int(data.pop("src"))
            data["dst_port"] = int(data.pop("dst", data["src_port"]))
        return data

    @model_validator(mode="after")
    def require_at_least_one_protocol(self) -> "PortForward":
        # Runs after handle_legacy_schema has already normalised the dict,
        # so self.tcp and self.udp are already coerced to bool.
        if not self.tcp and not self.udp:
            raise ValueError("At least one of tcp/udp must be True")
        return self


class Forwards(BaseModel):
    local: list[PortForward] = []
    remote: list[PortForward] = []


class FileShare(BaseModel):
    """A persisted file share associated with a connection."""

    file_path: str
    password: str
    port: int = 0  # 0 = auto-assigned; written back after first start
    stopped: bool = False  # True when manually stopped — not auto-restarted


class Connection(BaseModel):
    tag: str
    ssh_host: str
    socks_proxy_port: int = 0
    enabled: bool = True
    forwards: Forwards = Forwards()
    pac_hosts: list[str] = []
    pac_hosts_disabled: list[str] = []
    file_shares: list[FileShare] = []


class AppConfig(BaseModel):
    stop_on_quit: bool = True
    ephemeral_ports: bool = False
    logo_style: LogoStyle = LogoStyle.COLORED_GLASSES
    restore_shares_on_start: bool = True
    status_server_port: int = 0

    @field_validator("stop_on_quit", "ephemeral_ports", "restore_shares_on_start", mode="before")
    @classmethod
    def coerce_bool_string(cls, v: Any) -> Any:
        """Handle "1"/"0" string values from old yq-based config."""
        if isinstance(v, str):
            return v.strip() in ("1", "true", "True", "yes")
        return v

    @field_validator("logo_style", mode="before")
    @classmethod
    def coerce_logo_style(cls, v: Any) -> Any:
        if isinstance(v, str):
            return LogoStyle(v)
        return v


class SusOpsConfig(BaseModel):
    pac_server_port: int = 0
    connections: list[Connection] = []
    susops_app: AppConfig = AppConfig()


def get_config_path(workspace: Path = WORKSPACE_DEFAULT) -> Path:
    """Return the path to config.yaml within the given workspace directory."""
    return workspace / CONFIG_FILENAME


def load_config(workspace: Path = WORKSPACE_DEFAULT) -> SusOpsConfig:
    """Load config from workspace/config.yaml. Creates default config if missing."""
    path = get_config_path(workspace)
    if not path.exists():
        config = SusOpsConfig()
        save_config(config, workspace)
        return config
    yaml = YAML()
    data = yaml.load(path)
    if data is None:
        return SusOpsConfig()
    return SusOpsConfig.model_validate(dict(data))


def save_config(config: SusOpsConfig, workspace: Path = WORKSPACE_DEFAULT) -> None:
    """Save config to workspace/config.yaml using ruamel.yaml for comment preservation.

    Writes atomically (to a temp file, then POSIX rename) so a concurrent
    load_config never observes a half-written or freshly-truncated file. The
    old behaviour — `open(path, 'w')` — truncated the file to 0 bytes before
    `yaml.dump` ran; a reader in that window got an empty file → empty config →
    and any save that followed wiped the connections list. The TUI's
    `@work(thread=True)` makes this a real (and reported) race on rapid Stop
    clicks.
    """
    import os
    path = get_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    # Convert to plain dict via model_dump, then save
    data = config.model_dump(mode='python')
    # Convert enums to their values for serialization
    data['susops_app']['logo_style'] = config.susops_app.logo_style.value
    # Compute the target mode BEFORE writing: preserve any user-set restrictive
    # permissions (e.g. chmod 600 on a hardened install) since Path.replace()
    # would otherwise clobber them with the temp file's umask-derived mode.
    # New configs default to 0o600 — the file holds share passwords.
    try:
        target_mode = path.stat().st_mode & 0o777
    except OSError:
        target_mode = 0o600
    # Temp file in the same directory so the rename stays on one filesystem.
    # Open with O_EXCL so we never overwrite a leftover temp file from a
    # crashed earlier save (would otherwise inherit its mode).
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            target_mode,
        )
        try:
            with os.fdopen(fd, 'w') as f:
                yaml.dump(data, f)
        except Exception:
            # fdopen owns fd on success; on failure we may need to close it
            # if fdopen never took ownership. Best-effort.
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        # umask may have stripped bits from O_CREAT's mode argument — set
        # explicitly so the result matches target_mode exactly.
        os.chmod(tmp_path, target_mode)
        tmp_path.replace(path)  # atomic on POSIX + Windows ≥ 3.3
    except Exception:
        # Best-effort cleanup of the temp file if the write failed.
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def get_connection(config: SusOpsConfig, tag: str) -> Connection | None:
    """Find a connection by tag."""
    return next((c for c in config.connections if c.tag == tag), None)


def get_default_connection(config: SusOpsConfig) -> Connection | None:
    """Return the first connection, or None if there are none."""
    return config.connections[0] if config.connections else None
