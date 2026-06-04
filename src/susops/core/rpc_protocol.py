"""JSON-over-HTTP RPC protocol for the susops-services daemon.

Encodes facade arguments / return values losslessly. Pydantic models are
serialized via model_dump(); dataclasses via asdict(); enums via .value
with a type tag so the client can rebuild the exact type. Anything else
serializable by json.dumps passes through unchanged.
"""
from __future__ import annotations

import dataclasses
import enum
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Registry of known reconstructable types. Populated lazily to avoid
# import cycles with the facade.
_REGISTRY: dict[str, type] = {}


def _registry() -> dict[str, type]:
    if _REGISTRY:
        return _REGISTRY
    from susops.core.config import (
        AppConfig,
        Connection,
        FileShare,
        Forwards,
        PortForward,
        SusOpsConfig,
    )
    from susops.core.types import (
        ConnectionStatus,
        LogoStyle,
        ProcessState,
        ShareInfo,
        StartResult,
        StatusResult,
        StopResult,
        TestResult,
    )
    _REGISTRY.update({
        "Connection": Connection,
        "FileShare": FileShare,
        "PortForward": PortForward,
        "SusOpsConfig": SusOpsConfig,
        "AppConfig": AppConfig,
        "Forwards": Forwards,
        "ConnectionStatus": ConnectionStatus,
        "LogoStyle": LogoStyle,
        "ProcessState": ProcessState,
        "ShareInfo": ShareInfo,
        "StartResult": StartResult,
        "StatusResult": StatusResult,
        "StopResult": StopResult,
        "TestResult": TestResult,
        "Path": Path,
    })
    return _REGISTRY


def encode_value(v: Any) -> Any:
    """Recursively encode a Python value to a JSON-safe structure."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Path):
        return {"__type__": "Path", "value": str(v)}
    # Enums BEFORE BaseModel/dataclass checks so a Pydantic-model-with-enum-field
    # won't accidentally match the model branch.
    if isinstance(v, enum.Enum):
        return {"__type__": type(v).__name__, "value": v.value}
    if isinstance(v, BaseModel):
        return {"__type__": type(v).__name__, "value": v.model_dump(mode="json")}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        # Encode field-by-field via encode_value rather than dataclasses.asdict()
        # — asdict() recursively flattens nested dataclasses (and tuples) into
        # plain dicts/lists, throwing away the __type__ tags the decoder needs
        # to reconstruct the original types. Going through encode_value keeps
        # ConnectionStatus / TestResult / etc. tagged inside their parents.
        encoded = {
            f.name: encode_value(getattr(v, f.name))
            for f in dataclasses.fields(v)
        }
        return {"__type__": type(v).__name__, "value": encoded}
    if isinstance(v, (list, tuple)):
        return [encode_value(x) for x in v]
    if isinstance(v, dict):
        return _encode_dict(v)
    raise TypeError(f"Cannot encode value of type {type(v).__name__}: {v!r}")


def _encode_dict(d: dict) -> dict:
    return {k: encode_value(val) for k, val in d.items()}


def decode_arg(v: Any) -> Any:
    """Recursively decode a JSON-safe structure back into Python objects."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, list):
        return [decode_arg(x) for x in v]
    if isinstance(v, dict):
        if "__type__" in v and "value" in v:
            return _decode_tagged(v["__type__"], v["value"])
        return {k: decode_arg(val) for k, val in v.items()}
    return v


def _decode_tagged(type_name: str, value: Any) -> Any:
    cls = _registry().get(type_name)
    if cls is None:
        raise ValueError(f"Unknown RPC type tag: {type_name}")
    if cls is Path:
        return Path(value)
    if isinstance(cls, type) and issubclass(cls, enum.Enum):
        return cls(value)
    if isinstance(cls, type) and issubclass(cls, BaseModel):
        return cls.model_validate(value)
    if dataclasses.is_dataclass(cls):
        # Recursively decode nested fields so dataclasses-containing-models work.
        kwargs = {}
        for name, raw in value.items():
            if isinstance(raw, dict) and "__type__" in raw:
                kwargs[name] = decode_arg(raw)
            elif isinstance(raw, list):
                kwargs[name] = [decode_arg(x) for x in raw]
            else:
                kwargs[name] = raw
        return cls(**kwargs)
    raise ValueError(f"Don't know how to reconstruct type: {type_name}")


@dataclasses.dataclass
class InvocationRequest:
    method: str
    args: list = dataclasses.field(default_factory=list)
    kwargs: dict = dataclasses.field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "method": self.method,
            "args": encode_value(self.args),
            "kwargs": encode_value(self.kwargs),
        })

    @classmethod
    def from_json(cls, payload: str) -> "InvocationRequest":
        data = json.loads(payload)
        return cls(
            method=data["method"],
            args=[decode_arg(a) for a in data.get("args", [])],
            kwargs={k: decode_arg(v) for k, v in data.get("kwargs", {}).items()},
        )


@dataclasses.dataclass
class InvocationResponse:
    ok: bool
    result: Any = None
    error: str | None = None
    error_type: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "ok": self.ok,
            "result": encode_value(self.result),
            "error": self.error,
            "error_type": self.error_type,
        })

    @classmethod
    def from_json(cls, payload: str) -> "InvocationResponse":
        data = json.loads(payload)
        return cls(
            ok=data["ok"],
            result=decode_arg(data.get("result")),
            error=data.get("error"),
            error_type=data.get("error_type"),
        )
