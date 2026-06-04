"""OpenAPI freshness check.

This test re-runs ``tools/gen_openapi.py --check`` and fails if the committed
``docs/openapi.yaml`` no longer matches what the generator would produce from
the current ``SusOpsManager`` + ``_ALLOWED_METHODS``. When this fires, run
``python tools/gen_openapi.py`` locally and commit the regenerated spec.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_openapi_spec_is_up_to_date():
    """`tools/gen_openapi.py --check` must pass on the committed spec."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_openapi.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        "docs/openapi.yaml is stale.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}\n\n"
        "Run: python tools/gen_openapi.py  — and commit the result."
    )
