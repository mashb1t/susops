#!/usr/bin/env python3
"""Bump pkgver, pkgrel, and sha256sums in the AUR PKGBUILD for a new release.

Usage:
    python scripts/update_aur_pkgver.py 3.1.0
"""
from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

PKGBUILD = Path("packaging/aur/PKGBUILD")
_GITHUB_TARBALL = "https://github.com/mashb1t/susops/archive/v{version}.tar.gz"


def fetch_sha256(url: str) -> str:
    """Download *url* and return sha256 hex digest of its content."""
    with urllib.request.urlopen(url) as resp:
        return hashlib.sha256(resp.read()).hexdigest()


def update_pkgbuild(pkgbuild_path: Path, version: str, sha256: str) -> None:
    """Patch pkgver, reset pkgrel=1, and update sha256sums in *pkgbuild_path*."""
    if not re.match(r"^\d+\.\d+\.\d+", version):
        raise ValueError(f"version must be in X.Y.Z format, got {version!r}")
    content = pkgbuild_path.read_text()
    content = re.sub(r"^pkgver=.*", f"pkgver={version}", content, flags=re.MULTILINE)
    content = re.sub(r"^pkgrel=.*", "pkgrel=1", content, flags=re.MULTILINE)
    content = re.sub(
        r"^sha256sums=\(.*?\)",
        f"sha256sums=('{sha256}')",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )
    pkgbuild_path.write_text(content)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} VERSION", file=sys.stderr)
        sys.exit(1)
    version = sys.argv[1].lstrip("v")
    tarball_url = _GITHUB_TARBALL.format(version=version)
    print(f"Fetching {tarball_url} ...", flush=True)
    sha = fetch_sha256(tarball_url)
    print(f"sha256: {sha}")
    update_pkgbuild(PKGBUILD, version, sha)
    print(f"Updated {PKGBUILD}")
    print()
    print("Next steps:")
    print("  cd packaging/aur && makepkg --printsrcinfo > .SRCINFO")
    print(f"  git add PKGBUILD .SRCINFO && git commit -m 'chore(aur): bump to v{version}'")
    print("  git push aur main")


if __name__ == "__main__":
    main()
