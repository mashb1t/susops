#!/usr/bin/env python3
"""Update the main package sha256 in the Homebrew formula for a new release.

Usage:
    python scripts/update_homebrew_sha.py 3.1.0

This fetches the release tarball from GitHub, computes its sha256, and
patches packaging/homebrew/Formula/susops.rb in place.

Resource sha256s (pydantic, textual, etc.) must be updated separately —
run: brew fetch --build-from-source susops
then copy the printed sha256 values into the formula.
"""
from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

FORMULA = Path("packaging/homebrew/Formula/susops.rb")
GITHUB_URL = "https://github.com/mashb1t/susops/archive/v{version}.tar.gz"


def sha256_of_url(url: str) -> str:
    print(f"Fetching {url} ...", flush=True)
    with urllib.request.urlopen(url) as response:
        return hashlib.sha256(response.read()).hexdigest()


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} VERSION", file=sys.stderr)
        sys.exit(1)

    version = sys.argv[1].lstrip("v")
    tarball_url = GITHUB_URL.format(version=version)

    sha = sha256_of_url(tarball_url)
    print(f"sha256: {sha}")

    content = FORMULA.read_text()

    # Update the url line
    content = re.sub(
        r'url "https://github.com/mashb1t/susops/archive/v[^"]*"',
        f'url "https://github.com/mashb1t/susops/archive/v{version}.tar.gz"',
        content,
    )

    # Update the first sha256 line (the main package; resource shas follow)
    content = re.sub(
        r'sha256 "[^"]*"',
        f'sha256 "{sha}"',
        content,
        count=1,
    )

    FORMULA.write_text(content)
    print(f"Updated {FORMULA}")
    print()
    print("Next: update resource sha256s manually or via:")
    print("  brew fetch --build-from-source susops")


if __name__ == "__main__":
    main()
