#!/usr/bin/env python3
"""Compute sha256 for all PyPI resources needed in the Homebrew formula.

Usage:
    python scripts/compute_resource_shas.py

Outputs tab-separated: name  version  sha256  url
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

_DEV_PACKAGES: frozenset[str] = frozenset({
    "susops",
    "pip", "setuptools", "wheel", "build",
    "pytest", "pytest-cov", "coverage", "pluggy", "iniconfig",
    "textual-dev", "textual-serve",
    "msgpack", "aiohttp-jinja2", "jinja2", "markupsafe",
    "packaging",
})


def get_pypi_sdist(name: str, version: str) -> dict | None:
    """Return {url, sha256} for the sdist of name==version, or None."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json") as resp:
        data = json.loads(resp.read())
    return next(
        ({"url": u["url"], "sha256": u["digests"]["sha256"]}
         for u in data["urls"] if u["packagetype"] == "sdist"),
        None,
    )


def compute_resource_shas() -> dict[str, dict[str, str]]:
    """Return {name: {version, sha256, url}} for all formula resources."""
    out = subprocess.run(
        ["pip", "list", "--format=json"],
        capture_output=True, text=True, check=True,
    )
    packages = {p["name"].lower(): p["version"] for p in json.loads(out.stdout)}

    resources: dict[str, dict[str, str]] = {}
    for name, version in sorted(packages.items()):
        if name in _DEV_PACKAGES:
            continue
        sdist = get_pypi_sdist(name, version)
        if sdist:
            resources[name] = {"version": version, **sdist}
        else:
            print(f"WARNING: no sdist for {name}=={version}", file=sys.stderr)
    return resources


def main() -> None:
    for name, info in compute_resource_shas().items():
        print(f"{name}\t{info['version']}\t{info['sha256']}\t{info['url']}")


if __name__ == "__main__":
    main()
