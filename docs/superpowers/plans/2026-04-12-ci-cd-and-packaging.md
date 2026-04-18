# CI/CD and Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GitHub Actions CI (run tests on every PR/push) and a release workflow (build wheel, publish to PyPI, attach to GitHub release), plus a script to update Homebrew formula sha256 checksums for new releases.

**Architecture:** Two workflow files under `.github/workflows/`. Tests use `uv` (already in the repo via `uv.lock`). The release workflow triggers on `v*` tags. A standalone Python script generates sha256 checksums for the Homebrew formula's main package URL.

**Tech Stack:** GitHub Actions, uv, PyPI (via `uv publish`), Python `hashlib` + `urllib.request`

---

## File Map

- Create: `.github/workflows/test.yml` — run pytest on push and PR
- Create: `.github/workflows/release.yml` — build + publish on `v*` tag push
- Create: `scripts/update_homebrew_sha.py` — compute sha256 for a release tarball

---

### Task 1: Test workflow

**Files:**
- Create: `.github/workflows/test.yml`

- [ ] **Step 1: Create `.github/workflows/` directory**

```bash
mkdir -p .github/workflows
```

- [ ] **Step 2: Write `.github/workflows/test.yml`**

```yaml
name: Tests

on:
  push:
    branches: ["main", "master", "feature/**"]
  pull_request:
    branches: ["main", "master"]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12", "3.13", "3.14"]

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Set up Python ${{ matrix.python-version }}
        run: uv python install ${{ matrix.python-version }}

      - name: Install dependencies
        run: uv sync --extra dev --extra share

      - name: Run tests
        run: uv run pytest --cov=susops --cov-report=xml -v

      - name: Upload coverage report
        uses: codecov/codecov-action@v4
        if: matrix.python-version == '3.12'
        with:
          files: coverage.xml
          fail_ci_if_error: false
```

- [ ] **Step 3: Verify the file parses as valid YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/test.yml'))" && echo "OK"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: add GitHub Actions test workflow (Python 3.11–3.14, uv)"
```

---

### Task 2: Release workflow

Triggered on `v*` tag push. Builds a wheel, creates a GitHub release with the wheel attached, and publishes to PyPI.

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Write `.github/workflows/release.yml`**

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

permissions:
  contents: write  # needed to create GitHub release and upload assets

jobs:
  release:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Set up Python
        run: uv python install 3.12

      - name: Install dependencies
        run: uv sync --extra dev --extra share

      - name: Run tests
        run: uv run pytest -x -q

      - name: Build wheel and sdist
        run: uv build

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: dist/*
          generate_release_notes: true

      - name: Publish to PyPI
        run: uv publish
        env:
          UV_PUBLISH_TOKEN: ${{ secrets.PYPI_TOKEN }}
```

Note: `PYPI_TOKEN` must be added as a repository secret in GitHub Settings → Secrets → Actions.

- [ ] **Step 2: Verify the file parses as valid YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml'))" && echo "OK"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: add release workflow — build, GitHub release, PyPI publish on v* tag"
```

---

### Task 3: Homebrew sha256 update script

The Homebrew formula at `packaging/homebrew/Formula/susops.rb` has `sha256 "PLACEHOLDER"` for the main package URL. This script fetches the release tarball from GitHub and prints the correct sha256, then patches the formula file.

**Files:**
- Create: `scripts/update_homebrew_sha.py`

- [ ] **Step 1: Create `scripts/` directory**

```bash
mkdir -p scripts
```

- [ ] **Step 2: Write `scripts/update_homebrew_sha.py`**

```python
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
```

- [ ] **Step 3: Make executable**

```bash
chmod +x scripts/update_homebrew_sha.py
```

- [ ] **Step 4: Smoke-test with a dry run (no network needed — just verify it imports and parses args)**

```bash
python3 scripts/update_homebrew_sha.py 2>&1 | grep -q "Usage:" && echo "arg check OK"
```

Expected: `arg check OK`

- [ ] **Step 5: Commit**

```bash
git add scripts/update_homebrew_sha.py
git commit -m "chore: add Homebrew formula sha256 update script"
```

---

### Task 4: AUR release script

The AUR PKGBUILD at `packaging/aur/PKGBUILD` uses `sha256sums=('SKIP')` so no checksum computation is needed. The release script only needs to bump `pkgver` and reset `pkgrel` to 1.

**Files:**
- Create: `scripts/update_aur_pkgver.py`

- [ ] **Step 1: Write `scripts/update_aur_pkgver.py`**

```python
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
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/update_aur_pkgver.py
```

- [ ] **Step 3: Smoke-test arg validation**

```bash
python3 scripts/update_aur_pkgver.py 2>&1 | grep -q "Usage:" && echo "arg check OK"
python3 scripts/update_aur_pkgver.py notaversion 2>&1 | grep -q "X.Y.Z" && echo "version check OK"
```

Expected: both `OK`

- [ ] **Step 4: Test it updates the PKGBUILD correctly**

```bash
cp packaging/aur/PKGBUILD packaging/aur/PKGBUILD.bak
python3 scripts/update_aur_pkgver.py 9.9.9
grep "pkgver=9.9.9" packaging/aur/PKGBUILD && echo "pkgver OK"
grep "pkgrel=1" packaging/aur/PKGBUILD && echo "pkgrel OK"
cp packaging/aur/PKGBUILD.bak packaging/aur/PKGBUILD && rm packaging/aur/PKGBUILD.bak
```

Expected: both `OK`

- [ ] **Step 5: Commit**

```bash
git add scripts/update_aur_pkgver.py
git commit -m "chore: add AUR PKGBUILD version bump script"
```
