#!/usr/bin/env python3
"""Regenerate ``docs/openapi.yaml`` from ``SusOpsManager`` + the RPC allowlist.

Run this on every interface change — preferably as a pre-commit hook. The CI
test ``tests/test_openapi.py`` re-runs the generator and asserts the result
matches the committed file, so a stale spec fails the build.

Strategy:
    For each method in ``_ALLOWED_METHODS``:
        1. Introspect the signature via ``inspect.signature`` +
           ``typing.get_type_hints`` (resolves PEP 604 unions / forward refs).
        2. Build a JSON Schema for each parameter using Pydantic v2's
           ``TypeAdapter`` — this handles Pydantic models, dataclasses, enums,
           primitives, unions, and ``Optional`` uniformly.
        3. Build a JSON Schema for the return type the same way.
        4. Emit one ``InvokeFoo`` request schema (envelope wrapping the typed
           args/kwargs) and one ``FooResult`` response result schema.

The full document is emitted as OpenAPI 3.1 (preferred for JSON Schema 2020-12
compatibility, which Pydantic v2 targets).

Usage:
    python tools/gen_openapi.py          # writes docs/openapi.yaml
    python tools/gen_openapi.py --check  # exits non-zero if file is stale
"""
from __future__ import annotations

import argparse
import inspect
import sys
import typing
from io import StringIO
from pathlib import Path

from pydantic import TypeAdapter
from ruamel.yaml import YAML

# Ensure the in-tree package is importable when running from a fresh checkout.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from packaging.version import Version  # noqa: E402

from susops import __version__ as _RAW_VERSION  # noqa: E402

# Normalise to the PEP 440 canonical form so the spec is byte-identical
# regardless of how the package was installed when generated. version.py
# returns the raw pyproject string from a source checkout but the normalised
# wheel-metadata form when installed — without this the committed spec flaps
# between "3.0.0-rc6.dev2" and "3.0.0rc6.dev2" depending on the environment.
SUSOPS_VERSION = str(Version(_RAW_VERSION))  # noqa: E402
from susops.core.rpc_server import _ALLOWED_METHODS  # noqa: E402
from susops.facade import SusOpsManager  # noqa: E402

OUTPUT = ROOT / "docs" / "openapi.yaml"

# Annotations we want to normalise before handing them to Pydantic. Most of
# the work is automatic; this map only catches things Pydantic chokes on or
# that we want to render more clearly.
_TYPE_OVERRIDES: dict[object, dict] = {
    Path: {"type": "string", "format": "path"},
    type(None): {"type": "null"},
}


def _schema_for(annotation: object) -> dict:
    """Return a JSON Schema dict for the given Python annotation.

    Falls back to an empty schema (matches anything) when we can't introspect
    — better than crashing on edge cases like ``**kwargs: Any``.
    """
    if annotation in (inspect.Parameter.empty, typing.Any):
        return {}
    if annotation in _TYPE_OVERRIDES:
        return dict(_TYPE_OVERRIDES[annotation])
    try:
        return TypeAdapter(annotation).json_schema(
            ref_template="#/components/schemas/{model}",
        )
    except Exception:
        # Pydantic raises for unsupported types (e.g. raw ``tuple`` without
        # element annotations). Stay permissive.
        return {}


def _kwargs_schema_for(method: typing.Callable) -> tuple[dict, list[str]]:
    """Build a JSON Schema for the kwargs object of an RPC method.

    Returns (schema, required-list). ``self`` is dropped. ``*args`` / ``**kwargs``
    are flagged in the description but not enumerated as properties.
    """
    sig = inspect.signature(method)
    try:
        hints = typing.get_type_hints(method)
    except Exception:
        hints = {}

    properties: dict[str, dict] = {}
    required: list[str] = []
    extras_note = []

    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            extras_note.append("accepts arbitrary positional args via *args")
            continue
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            extras_note.append("accepts arbitrary keyword args via **kwargs")
            continue

        ann = hints.get(name, param.annotation)
        prop_schema = _schema_for(ann)
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            default = param.default
            if isinstance(default, (str, int, float, bool)) or default is None:
                prop_schema = {**prop_schema, "default": default}
        properties[name] = prop_schema

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    schema["additionalProperties"] = bool(extras_note)
    if extras_note:
        schema["description"] = "; ".join(extras_note)
    return schema, required


def _result_schema_for(method: typing.Callable) -> dict:
    """JSON Schema for the method's return annotation."""
    try:
        hints = typing.get_type_hints(method)
    except Exception:
        return {}
    ret = hints.get("return", inspect.Parameter.empty)
    if ret is inspect.Parameter.empty or ret is None:
        return {"type": "null"}
    if ret is type(None):
        return {"type": "null"}
    return _schema_for(ret)


def _build_paths() -> tuple[dict, dict]:
    """Return (paths, named_component_schemas).

    The single ``POST /rpc`` endpoint is described with a ``oneOf`` body union
    discriminated by ``method``. Each method gets its own ``InvokeXxx``
    request schema and ``XxxResult`` response result schema for clarity.
    """
    one_of: list[dict] = []
    method_doc_lines: list[str] = []
    schemas: dict[str, dict] = {}

    for method_name in sorted(_ALLOWED_METHODS):
        method = getattr(SusOpsManager, method_name, None)
        if method is None or not callable(method):
            continue

        kwargs_schema, _required = _kwargs_schema_for(method)
        result_schema = _result_schema_for(method)
        doc = (inspect.getdoc(method) or "").strip().split("\n\n", 1)[0]

        invoke_name = f"Invoke{_pascal(method_name)}"
        result_name = f"{_pascal(method_name)}Result"

        schemas[invoke_name] = {
            "type": "object",
            "description": doc or f"Invoke `{method_name}` on SusOpsManager.",
            "required": ["method", "args", "kwargs"],
            "properties": {
                "method": {"type": "string", "const": method_name},
                "args": {
                    "type": "array",
                    "description": (
                        "Positional arguments. Mapped onto the method's named "
                        "parameters in order. Typically empty — prefer "
                        "`kwargs`."
                    ),
                    "items": {},
                },
                "kwargs": kwargs_schema,
            },
        }
        schemas[result_name] = result_schema
        one_of.append({"$ref": f"#/components/schemas/{invoke_name}"})

        method_doc_lines.append(f"- `{method_name}` — {doc}" if doc else f"- `{method_name}`")

    request_schema = {
        "type": "object",
        "oneOf": one_of,
        "discriminator": {"propertyName": "method"},
    }
    schemas["InvocationRequest"] = request_schema
    schemas["InvocationResponseSuccess"] = {
        "type": "object",
        "required": ["ok"],
        "properties": {
            "ok": {"type": "boolean", "const": True},
            "result": {
                "description": (
                    "Method return value. Type-tagged via rpc_protocol.py — "
                    "Pydantic models, dataclasses, enums, sets, and Paths are "
                    "all serialised as JSON-friendly forms with a `__type__` "
                    "discriminator. See the per-method `XxxResult` schemas."
                ),
            },
        },
    }
    schemas["InvocationResponseError"] = {
        "type": "object",
        "required": ["ok", "error", "error_type"],
        "properties": {
            "ok": {"type": "boolean", "const": False},
            "error": {"type": "string", "description": "Error message."},
            "error_type": {
                "type": "string",
                "description": (
                    "Python exception class name. Clients map known names "
                    "(ValueError, RuntimeError, FileNotFoundError, "
                    "PermissionError, KeyError, AttributeError) back to the "
                    "matching exception; everything else falls back to "
                    "RuntimeError."
                ),
            },
        },
    }

    paths = {
        "/rpc": {
            "post": {
                "summary": "Invoke a SusOpsManager method",
                "description": (
                        "Single endpoint dispatch. The request body's `method` "
                        "field selects which `SusOpsManager.<method>(*args, "
                        "**kwargs)` is invoked.\n\n"
                        "**Allowlisted methods:**\n\n"
                        + "\n".join(method_doc_lines)
                ),
                "operationId": "invoke",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/InvocationRequest"},
                        },
                    },
                },
                "responses": {
                    "200": {
                        "description": "Method invoked successfully.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvocationResponseSuccess"},
                            },
                        },
                    },
                    "400": {
                        "description": "Malformed request body.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvocationResponseError"},
                            },
                        },
                    },
                    "404": {
                        "description": "Method not on the allowlist.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvocationResponseError"},
                            },
                        },
                    },
                    "500": {
                        "description": "Method raised an exception.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/InvocationResponseError"},
                            },
                        },
                    },
                },
            },
        },
        "/events": {
            "get": {
                "summary": "Subscribe to live status events (SSE)",
                "description": (
                    "Long-lived Server-Sent Events stream. The daemon pushes "
                    "events of type `state`, `share`, `forward`, and "
                    "`bandwidth` whenever the underlying facade emits them. "
                    "Frontends use this to update icons / dashboards without "
                    "polling.\n\n"
                    "Event format follows the standard SSE wire format:\n\n"
                    "```\n"
                    "event: state\n"
                    "data: {\"tag\":\"work\",\"running\":true,\"pid\":1234}\n"
                    "\n"
                    "```\n\n"
                    "Closing the stream is the canonical \"I'm leaving\" "
                    "signal — when the last SSE client disconnects and the "
                    "daemon has no in-flight work, it shuts down."
                ),
                "operationId": "events",
                "responses": {
                    "200": {
                        "description": "Streaming SSE response.",
                        "content": {
                            "text/event-stream": {
                                "schema": {"$ref": "#/components/schemas/StatusEvent"},
                            },
                        },
                    },
                },
            },
        },
    }

    schemas["StatusEvent"] = {
        "oneOf": [
            {"$ref": "#/components/schemas/StateEvent"},
            {"$ref": "#/components/schemas/ShareEvent"},
            {"$ref": "#/components/schemas/ForwardEvent"},
            {"$ref": "#/components/schemas/BandwidthEvent"},
        ],
    }
    schemas["StateEvent"] = {
        "type": "object",
        "description": "Connection state change.",
        "required": ["tag", "running"],
        "properties": {
            "tag": {"type": "string"},
            "running": {"type": "boolean"},
            "pid": {"type": ["integer", "null"]},
            "reconnecting": {"type": "boolean"},
        },
    }
    schemas["ShareEvent"] = {
        "type": "object",
        "description": "Share lifecycle change.",
        "required": ["port"],
        "properties": {
            "port": {"type": "integer"},
            "file": {"type": "string"},
            "running": {"type": "boolean"},
            "conn_tag": {"type": "string"},
        },
    }
    schemas["ForwardEvent"] = {
        "type": "object",
        "description": "Port-forward registration / cancellation.",
        "required": ["tag"],
        "properties": {
            "tag": {"type": "string"},
            "fw_tag": {"type": "string"},
            "direction": {"type": "string", "enum": ["local", "remote"]},
            "running": {"type": "boolean"},
        },
    }
    schemas["BandwidthEvent"] = {
        "type": "object",
        "description": "Periodic bandwidth sample.",
        "required": ["tag", "rx_bps", "tx_bps"],
        "properties": {
            "tag": {"type": "string"},
            "rx_bps": {"type": "number"},
            "tx_bps": {"type": "number"},
        },
    }

    return paths, schemas


def _pascal(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


def build_openapi() -> dict:
    paths, schemas = _build_paths()
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "SusOps Services Daemon RPC",
            "version": SUSOPS_VERSION,
            "description": (
                "Internal HTTP API exposed by the `susops-services` daemon. "
                "Frontends (CLI, TUI, tray) call into it via the "
                "`SusOpsClient` proxy — there is no expectation that this "
                "API be consumed by anything else, but documenting it makes "
                "the contract explicit and lets you build alternative "
                "clients.\n\n"
                "**Bind address:** `127.0.0.1` — loopback only.\n\n"
                "**Authentication:** none. Any local process that can reach "
                "loopback and read `~/.susops/pids/susops-services.port` can "
                "invoke any allowlisted method. See the README's "
                "[Access & Authentication](https://github.com/mashb1t/susops"
                "#access--authentication) section."
            ),
            "x-generator": "tools/gen_openapi.py",
        },
        "servers": [
            {
                "url": "http://127.0.0.1:{port}",
                "description": (
                    "Local daemon. `port` is allocated at startup and "
                    "written to `~/.susops/pids/susops-services.port`."
                ),
                "variables": {
                    "port": {"default": "0", "description": "Daemon RPC port."},
                },
            },
        ],
        "paths": paths,
        "components": {"schemas": schemas},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Fail with non-zero exit if the generated spec differs from "
             "the committed file. Used by CI to detect stale OpenAPI.",
    )
    args = parser.parse_args()

    spec = build_openapi()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 100
    yaml.allow_unicode = True
    buf = StringIO()
    yaml.dump(spec, buf)
    rendered = buf.getvalue()

    if args.check:
        if not OUTPUT.exists():
            print(f"FAIL: {OUTPUT} does not exist — run `python tools/gen_openapi.py`",
                  file=sys.stderr)
            return 1
        existing = OUTPUT.read_text()
        if existing != rendered:
            print(
                f"FAIL: {OUTPUT} is stale. Run `python tools/gen_openapi.py` "
                "to regenerate and commit the result.",
                file=sys.stderr,
            )
            return 1
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered)
    print(f"Wrote {OUTPUT} ({len(rendered):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
