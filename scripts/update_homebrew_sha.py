#!/usr/bin/env python3
"""Update sha256 values in the Homebrew Formula and Cask for a new release.

Usage:
    python scripts/update_homebrew_sha.py 3.1.0
    python scripts/update_homebrew_sha.py 3.1.0 --dmg SusOps-3.1.0-arm64.dmg
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
from pathlib import Path

# Ensure sibling scripts are importable when run directly from repo root
_scripts_dir = str(Path(__file__).parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

FORMULA = Path("packaging/homebrew/Formula/susops.rb")
CASK    = Path("packaging/homebrew/Casks/susops.rb")
_GITHUB_TARBALL = "https://github.com/mashb1t/susops/archive/v{version}.tar.gz"


def sha256_of_url(url: str) -> str:
    print(f"Fetching {url} ...", flush=True)
    with urllib.request.urlopen(url) as resp:
        return hashlib.sha256(resp.read()).hexdigest()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def update_formula_main_sha(formula_path: Path, version: str, sha256: str) -> None:
    """Update the top-level url + first sha256 in the formula."""
    content = formula_path.read_text()
    content = re.sub(
        r'url "https://github\.com/mashb1t/susops/archive/v[^"]*"',
        f'url "https://github.com/mashb1t/susops/archive/v{version}.tar.gz"',
        content,
    )
    content = re.sub(r'sha256 "[^"]*"', f'sha256 "{sha256}"', content, count=1)
    formula_path.write_text(content)


def update_formula_resource_shas(
    formula_path: Path, shas: dict[str, dict[str, str]]
) -> None:
    """Patch each resource block's url and sha256."""
    content = formula_path.read_text()
    for name, info in shas.items():
        pattern = (
            rf'(resource "{re.escape(name)}" do\s*\n)'
            rf'(\s*url ")[^"]*(")\s*\n'
            rf'(\s*sha256 ")[^"]*(")'
        )
        replacement = rf'\1\2{info["url"]}\3\n\4{info["sha256"]}\5'
        content = re.sub(pattern, replacement, content)
    formula_path.write_text(content)


def update_cask_sha(cask_path: Path, version: str, sha256: str) -> None:
    """Pin the cask to a specific version + sha256."""
    content = cask_path.read_text()
    content = re.sub(r"version :latest", f'version "{version}"', content)
    content = re.sub(r"sha256 :no_check", f'sha256 "{sha256}"', content)
    content = re.sub(
        r'url "([^"]*)/releases/latest/download/SusOps-#\{version\}-arm64\.dmg"',
        f'url "https://github.com/mashb1t/susops/releases/download/v{version}/SusOps-{version}-arm64.dmg"',
        content,
    )
    cask_path.write_text(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--dmg", metavar="PATH")
    args = parser.parse_args()

    version = args.version.lstrip("v")

    try:
        main_sha = sha256_of_url(_GITHUB_TARBALL.format(version=version))
    except Exception as e:
        print(f"Error: Failed to fetch tarball: {e}", file=sys.stderr)
        sys.exit(1)
    update_formula_main_sha(FORMULA, version, main_sha)
    print(f"Updated {FORMULA} main sha256: {main_sha}")

    from compute_resource_shas import compute_resource_shas
    print("Resolving resource sha256s from PyPI ...")
    shas = compute_resource_shas()
    update_formula_resource_shas(FORMULA, shas)
    print(f"Updated {len(shas)} resource sha256s in {FORMULA}")

    if args.dmg:
        dmg_sha = sha256_of_file(Path(args.dmg))
        update_cask_sha(CASK, version, dmg_sha)
        print(f"Updated {CASK} sha256: {dmg_sha}")
    else:
        print("Skipping cask sha (no --dmg given)")


if __name__ == "__main__":
    main()
