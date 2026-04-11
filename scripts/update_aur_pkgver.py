#!/usr/bin/env python3
"""Bump pkgver (and reset pkgrel to 1) in the AUR PKGBUILD for a new release.

Usage:
    python scripts/update_aur_pkgver.py 3.1.0

After running:
    1. Verify packaging/aur/PKGBUILD looks correct
    2. cd packaging/aur && makepkg -si   # verify the package builds locally
    3. git add packaging/aur/PKGBUILD && git commit -m "chore(aur): bump to v3.1.0"
    4. Push to the AUR remote (separate git remote, not GitHub)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PKGBUILD = Path("packaging/aur/PKGBUILD")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} VERSION", file=sys.stderr)
        sys.exit(1)

    version = sys.argv[1].lstrip("v")
    if not re.match(r"^\d+\.\d+\.\d+", version):
        print(f"Error: version must be in X.Y.Z format, got {version!r}", file=sys.stderr)
        sys.exit(1)

    content = PKGBUILD.read_text()
    content = re.sub(r"^pkgver=.*", f"pkgver={version}", content, flags=re.MULTILINE)
    content = re.sub(r"^pkgrel=.*", "pkgrel=1", content, flags=re.MULTILINE)
    PKGBUILD.write_text(content)

    print(f"Updated {PKGBUILD}")
    print(f"  pkgver={version}")
    print(f"  pkgrel=1")
    print()
    print("Next steps:")
    print(f"  cd packaging/aur && makepkg -si")
    print(f"  git add PKGBUILD .SRCINFO && git commit -m 'chore(aur): bump to v{version}'")
    print(f"  git push aur main")


if __name__ == "__main__":
    main()
