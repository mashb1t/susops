# Homebrew + AUR Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate macOS (Homebrew Formula + Cask) and Arch Linux (AUR) packaging on every tagged GitHub release, with local helper scripts and full test coverage.

**Architecture:** A single `release.yml` workflow gains three new jobs — `build-dmg` (macOS arm64, PyInstaller), `update-tap` (patches `homebrew-susops` repo), and `update-aur` (patches AUR git repo). Scripts in `scripts/` are developer-facing helpers with the same logic as CI; they are unit-tested with mocked HTTP.

**Tech Stack:** Python 3.11+, PyInstaller, hdiutil (macOS), GitHub Actions, Arch Docker image for `.SRCINFO` generation, `urllib.request` + PyPI JSON API for sha256 resolution.

**Spec:** `docs/superpowers/specs/2026-04-23-packaging-design.md`

---

## Prerequisites (before starting tasks)

Commit the pending changes in `develop` (upgraded `uv.lock`, `pyproject.toml`, `facade.py`), then sync the worktree:

```bash
# In main working tree (develop branch):
git add pyproject.toml src/susops/facade.py uv.lock
git commit -m "chore: upgrade pydantic + textual, sync lockfile"

# In the worktree:
cd .worktrees/feature/packaging
git merge develop
uv sync --extra dev --extra share
```

---

## File Map

**Modified:**
- `src/susops/version.py` — replace AST-file reader with `importlib.metadata`
- `tests/conftest.py` — add `scripts/` to `sys.path` for script imports
- `packaging/aur/PKGBUILD` — fix `depends`, `optdepends`, `makedepends`, `sha256sums`
- `packaging/homebrew/Formula/susops.rb` — full rewrite: all resources, livecheck, `Language::Python::Virtualenv`
- `packaging/homebrew/Casks/susops.rb` — fix URL template to `arm64.dmg`, remove PLACEHOLDER
- `scripts/update_aur_pkgver.py` — add `fetch_sha256()`, `update_pkgbuild()` public functions
- `scripts/update_homebrew_sha.py` — add `update_resource_shas()`, `update_cask_sha()` public functions
- `.github/workflows/release.yml` — split existing `release` job; add `build-dmg`, `update-tap`, `update-aur`

**Created:**
- `packaging/aur/susops-tray.desktop` — XDG desktop entry for tray app
- `packaging/macos/entry_tray.py` — PyInstaller entry-point script
- `packaging/macos/susops.spec` — PyInstaller spec for SusOps.app
- `scripts/compute_resource_shas.py` — resolves full transitive dep tree, returns sha256 per package
- `tests/test_packaging.py` — smoke tests for packaging file structure
- `tests/test_scripts.py` — unit tests for scripts (mocked HTTP)
- `tests/test_version.py` — version importability tests

**Deleted:**
- `version.py` (root) — replaced by `importlib.metadata` in `src/susops/version.py`

---

## Task 1: Migrate version.py to importlib.metadata

**Files:**
- Modify: `src/susops/version.py`
- Delete: `version.py` (root)
- Create: `tests/test_version.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_version.py
def test_version_is_importable_string():
    from susops import __version__
    assert isinstance(__version__, str)
    assert __version__

def test_version_matches_pyproject():
    import tomllib
    from pathlib import Path
    from susops import __version__
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text()
    )
    assert __version__ == pyproject["project"]["version"]
```

- [ ] **Step 2: Run tests to verify the second fails**

```bash
uv run pytest tests/test_version.py -v
```

Expected: `test_version_is_importable_string` PASSES, `test_version_matches_pyproject` FAILS (`3.0.0-rc2` vs `3.0.0`).

- [ ] **Step 3: Replace src/susops/version.py**

```python
# src/susops/version.py
from importlib.metadata import version, PackageNotFoundError

try:
    VERSION = version("susops")
except PackageNotFoundError:
    # Running from source without installation — fall back to pyproject.toml
    import tomllib
    from pathlib import Path
    _pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"
    VERSION = tomllib.loads(_pyproject.read_text())["project"]["version"]
```

- [ ] **Step 4: Delete root version.py**

```bash
git rm version.py
```

- [ ] **Step 5: Run tests — both should pass**

```bash
uv run pytest tests/test_version.py -v
```

Expected: both PASS.

- [ ] **Step 6: Run full suite to check for regressions**

```bash
uv run pytest -x -q
```

Expected: 181+ tests passing.

- [ ] **Step 7: Commit**

```bash
git add src/susops/version.py tests/test_version.py
git commit -m "feat: migrate version.py to importlib.metadata, remove root version.py"
```

---

## Task 2: Write test_packaging.py (failing tests first)

**Files:**
- Create: `tests/test_packaging.py`

- [ ] **Step 1: Create tests/test_packaging.py**

```python
# tests/test_packaging.py
"""Smoke tests for packaging file structure and content correctness."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PKGBUILD  = REPO_ROOT / "packaging" / "aur" / "PKGBUILD"
FORMULA   = REPO_ROOT / "packaging" / "homebrew" / "Formula" / "susops.rb"
CASK      = REPO_ROOT / "packaging" / "homebrew" / "Casks" / "susops.rb"
DESKTOP   = REPO_ROOT / "packaging" / "aur" / "susops-tray.desktop"
SPEC      = REPO_ROOT / "packaging" / "macos" / "susops.spec"


def _pyproject_version() -> str:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]


def _pkgbuild_array(name: str) -> list[str]:
    text = PKGBUILD.read_text()
    m = re.search(rf"^{name}=\(([^)]*)\)", text, re.MULTILINE | re.DOTALL)
    return re.findall(r"'([^']+)'", m.group(1)) if m else []


# ── PKGBUILD ──────────────────────────────────────────────────────────────────

def test_pkgbuild_exists():
    assert PKGBUILD.exists()

def test_pkgbuild_depends_no_gtk():
    depends = _pkgbuild_array("depends")
    gtk_pkgs = {"python-gobject", "gtk3", "libayatana-appindicator"}
    assert not gtk_pkgs & set(depends)

def test_pkgbuild_depends_ruamel():
    assert "python-ruamel-yaml" in _pkgbuild_array("depends")

def test_pkgbuild_optdepends_has_gtk():
    names = {e.split(":")[0] for e in _pkgbuild_array("optdepends")}
    assert "python-gobject" in names
    assert "gtk3" in names

def test_pkgbuild_optdepends_has_textual():
    names = {e.split(":")[0] for e in _pkgbuild_array("optdepends")}
    assert "python-textual" in names

def test_pkgbuild_version_matches_pyproject():
    m = re.search(r"^pkgver=(.+)$", PKGBUILD.read_text(), re.MULTILINE)
    assert m and m.group(1) == _pyproject_version()

def test_desktop_file_exists():
    assert DESKTOP.exists()

def test_desktop_file_has_exec():
    assert "Exec=susops-tray" in DESKTOP.read_text()

# ── Homebrew Formula ──────────────────────────────────────────────────────────

def test_formula_exists():
    assert FORMULA.exists()

def test_formula_has_no_placeholder():
    assert "PLACEHOLDER" not in FORMULA.read_text()

def test_formula_has_livecheck():
    assert "livecheck" in FORMULA.read_text()

def test_formula_has_virtualenv_include():
    assert "Language::Python::Virtualenv" in FORMULA.read_text()

def test_formula_version_matches_pyproject():
    m = re.search(
        r'url "https://github\.com/mashb1t/susops/archive/v([^"]+)\.tar\.gz"',
        FORMULA.read_text(),
    )
    assert m and m.group(1) == _pyproject_version()

# ── Homebrew Cask ─────────────────────────────────────────────────────────────

def test_cask_exists():
    assert CASK.exists()

def test_cask_url_has_arm64():
    assert "arm64" in CASK.read_text()

def test_cask_version_interpolation():
    assert "#{version}" in CASK.read_text()

# ── PyInstaller spec ──────────────────────────────────────────────────────────

def test_pyinstaller_spec_exists():
    assert SPEC.exists()

def test_pyinstaller_spec_has_bundle():
    text = SPEC.read_text()
    assert "BUNDLE" in text
    assert "SusOps.app" in text
```

- [ ] **Step 2: Run tests to see current failures**

```bash
uv run pytest tests/test_packaging.py -v
```

Expected failures: `test_pkgbuild_depends_no_gtk`, `test_pkgbuild_depends_ruamel`, `test_pkgbuild_optdepends_*`, `test_pkgbuild_version_matches_pyproject`, `test_desktop_file_exists`, all formula/cask/spec tests.

- [ ] **Step 3: Commit**

```bash
git add tests/test_packaging.py
git commit -m "test: add packaging smoke tests (red — will green up per task)"
```

---

## Task 3: Add susops-tray.desktop and fix PKGBUILD

**Files:**
- Create: `packaging/aur/susops-tray.desktop`
- Modify: `packaging/aur/PKGBUILD`

- [ ] **Step 1: Create packaging/aur/susops-tray.desktop**

```ini
[Desktop Entry]
Name=SusOps Tray
Comment=SSH SOCKS5 proxy manager — system tray
Exec=susops-tray
Icon=susops
Type=Application
Categories=Network;Utility;
StartupNotify=false
```

- [ ] **Step 2: Rewrite packaging/aur/PKGBUILD**

Replace the entire file with:

```
# Maintainer: Manuel Schmid <manuel.schmid@odt.net>
pkgname=susops
pkgver=3.0.0
pkgrel=1
pkgdesc="SSH SOCKS5 proxy manager — Python TUI + system tray"
arch=('any')
url="https://github.com/mashb1t/susops"
license=('MIT')
depends=(
    'python'
    'python-pydantic'
    'python-psutil'
    'python-ruamel-yaml'
)
optdepends=(
    'python-textual: interactive TUI interface'
    'python-textual-plotext: bandwidth chart in TUI'
    'python-gobject: GTK3 system tray app'
    'gtk3: GTK3 system tray app'
    'libayatana-appindicator: GTK3 system tray app'
    'python-cryptography: encrypted file sharing'
    'python-aiohttp: file sharing and SSE status server'
    'socat: UDP port forwarding support'
)
makedepends=('python-build' 'python-installer' 'python-wheel')
source=("$pkgname-$pkgver.tar.gz::https://github.com/mashb1t/susops/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
    cd "$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$pkgname-$pkgver"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 packaging/aur/susops-tray.desktop \
        "$pkgdir/usr/share/applications/susops-tray.desktop"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
```

Note: `pkgver` is updated by `scripts/update_aur_pkgver.py` on release. `sha256sums=('SKIP')` is the dev placeholder; CI fills the real sha256.

- [ ] **Step 3: Align pkgver with pyproject.toml**

```bash
VER=$(python3 -c "import tomllib; print(tomllib.loads(open('pyproject.toml').read())['project']['version'])")
sed -i "s/^pkgver=.*/pkgver=$VER/" packaging/aur/PKGBUILD
```

- [ ] **Step 4: Run PKGBUILD and desktop tests**

```bash
uv run pytest tests/test_packaging.py -v -k "pkgbuild or desktop"
```

Expected: all 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add packaging/aur/PKGBUILD packaging/aur/susops-tray.desktop
git commit -m "feat(aur): fix PKGBUILD depends/optdepends, add susops-tray.desktop"
```

---

## Task 4: Rewrite Homebrew Formula

**Files:**
- Modify: `packaging/homebrew/Formula/susops.rb`

sha256 values are `PLACEHOLDER` here; Task 10 populates them via `compute_resource_shas.py`. The formula lists all ~30 transitive runtime deps required by `virtualenv_install_with_resources`.

- [ ] **Step 1: Rewrite packaging/homebrew/Formula/susops.rb**

```ruby
class Susops < Formula
  include Language::Python::Virtualenv

  desc "SSH SOCKS5 proxy manager — Python TUI + PAC server"
  homepage "https://github.com/mashb1t/susops"
  url "https://github.com/mashb1t/susops/archive/v3.0.0.tar.gz"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "socat"
  depends_on "python@3.12"

  livecheck do
    url :stable
    strategy :github_latest
  end

  # Generated by: python scripts/compute_resource_shas.py
  # Updated by:   python scripts/update_homebrew_sha.py <version>

  resource "aiohappyeyeballs" do
    url "https://files.pythonhosted.org/packages/source/a/aiohappyeyeballs/aiohappyeyeballs-2.6.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "aiohttp" do
    url "https://files.pythonhosted.org/packages/source/a/aiohttp/aiohttp-3.13.5.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "aiosignal" do
    url "https://files.pythonhosted.org/packages/source/a/aiosignal/aiosignal-1.4.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/source/a/annotated_types/annotated_types-0.7.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "attrs" do
    url "https://files.pythonhosted.org/packages/source/a/attrs/attrs-26.1.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "cffi" do
    url "https://files.pythonhosted.org/packages/source/c/cffi/cffi-1.17.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "cryptography" do
    url "https://files.pythonhosted.org/packages/source/c/cryptography/cryptography-46.0.7.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "frozenlist" do
    url "https://files.pythonhosted.org/packages/source/f/frozenlist/frozenlist-1.8.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/source/i/idna/idna-3.11.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "linkify-it-py" do
    url "https://files.pythonhosted.org/packages/source/l/linkify_it_py/linkify_it_py-2.1.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/source/m/markdown_it_py/markdown_it_py-4.0.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "mdit-py-plugins" do
    url "https://files.pythonhosted.org/packages/source/m/mdit_py_plugins/mdit_py_plugins-0.5.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/source/m/mdurl/mdurl-0.1.2.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "multidict" do
    url "https://files.pythonhosted.org/packages/source/m/multidict/multidict-6.7.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "platformdirs" do
    url "https://files.pythonhosted.org/packages/source/p/platformdirs/platformdirs-4.9.4.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "plotext" do
    url "https://files.pythonhosted.org/packages/source/p/plotext/plotext-5.3.2.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "propcache" do
    url "https://files.pythonhosted.org/packages/source/p/propcache/propcache-0.4.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "psutil" do
    url "https://files.pythonhosted.org/packages/source/p/psutil/psutil-7.2.2.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "pycparser" do
    url "https://files.pythonhosted.org/packages/source/p/pycparser/pycparser-2.22.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/source/p/pydantic/pydantic-2.12.5.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "pydantic-core" do
    url "https://files.pythonhosted.org/packages/source/p/pydantic_core/pydantic_core-2.41.5.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/source/p/pygments/pygments-2.20.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-14.3.3.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "ruamel-yaml" do
    url "https://files.pythonhosted.org/packages/source/r/ruamel.yaml/ruamel.yaml-0.19.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "textual" do
    url "https://files.pythonhosted.org/packages/source/t/textual/textual-8.2.3.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "textual-plotext" do
    url "https://files.pythonhosted.org/packages/source/t/textual_plotext/textual_plotext-1.0.1.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/source/t/typing_extensions/typing_extensions-4.15.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/source/t/typing_inspection/typing_inspection-0.4.2.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "uc-micro-py" do
    url "https://files.pythonhosted.org/packages/source/u/uc_micro_py/uc_micro_py-1.0.3.tar.gz"
    sha256 "PLACEHOLDER"
  end

  resource "yarl" do
    url "https://files.pythonhosted.org/packages/source/y/yarl/yarl-1.23.0.tar.gz"
    sha256 "PLACEHOLDER"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    output = shell_output("#{bin}/susops ps 2>&1", 3)
    assert_match "stopped", output
  end
end
```

- [ ] **Step 2: Run formula structure tests**

```bash
uv run pytest tests/test_packaging.py -v -k "formula"
```

Expected: `test_formula_exists`, `test_formula_has_livecheck`, `test_formula_has_virtualenv_include`, `test_formula_version_matches_pyproject` PASS. `test_formula_has_no_placeholder` still FAILS (fixed in Task 10).

- [ ] **Step 3: Commit**

```bash
git add packaging/homebrew/Formula/susops.rb
git commit -m "feat(homebrew): rewrite formula with all resources, livecheck, virtualenv include"
```

---

## Task 5: Fix Homebrew Cask

**Files:**
- Modify: `packaging/homebrew/Casks/susops.rb`

- [ ] **Step 1: Rewrite packaging/homebrew/Casks/susops.rb**

```ruby
cask "susops" do
  version :latest
  sha256 :no_check

  url "https://github.com/mashb1t/susops/releases/latest/download/SusOps-#{version}-arm64.dmg"
  name "SusOps"
  desc "SSH SOCKS5 proxy manager — macOS tray app"
  homepage "https://github.com/mashb1t/susops"

  app "SusOps.app"

  zap trash: [
    "~/.susops",
    "~/Library/Application Support/SusOps",
    "~/Library/Logs/SusOps",
  ]
end
```

Note: `version :latest` / `sha256 :no_check` are repo defaults. The `update-tap` CI job patches them to pinned values before pushing to the tap.

- [ ] **Step 2: Run cask tests**

```bash
uv run pytest tests/test_packaging.py -v -k "cask"
```

Expected: all 3 PASS.

- [ ] **Step 3: Commit**

```bash
git add packaging/homebrew/Casks/susops.rb
git commit -m "feat(homebrew): fix cask URL template for arm64 dmg release asset"
```

---

## Task 6: Create PyInstaller entry script and spec

**Files:**
- Create: `packaging/macos/entry_tray.py`
- Create: `packaging/macos/susops.spec`

- [ ] **Step 1: Create packaging/macos/entry_tray.py**

```python
# packaging/macos/entry_tray.py
"""PyInstaller entry point for the SusOps macOS tray app."""
from susops.tray import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create packaging/macos/susops.spec**

```python
# packaging/macos/susops.spec
# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SusOps macOS tray app (arm64).

Build (from repo root):
    pyinstaller packaging/macos/susops.spec --clean --noconfirm

Prerequisites:
    assets/susops.icns — generated by build-dmg CI job via sips + iconutil
    pip install pyinstaller rumps pyobjc
"""
from PyInstaller.utils.hooks import collect_all

rumps_datas, rumps_binaries, rumps_hiddenimports = collect_all("rumps")

a = Analysis(
    ["packaging/macos/entry_tray.py"],
    pathex=["."],
    binaries=rumps_binaries,
    datas=rumps_datas,
    hiddenimports=rumps_hiddenimports + [
        "objc",
        "Foundation",
        "AppKit",
        "Cocoa",
        "susops.tray",
        "susops.tray.mac",
        "susops.tray.base",
        "susops.facade",
        "susops.core.config",
        "susops.core.ssh",
        "susops.core.ports",
        "susops.core.types",
        "susops.core.process",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["textual", "textual_plotext", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SusOps",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=True,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/susops.icns",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SusOps",
)

app = BUNDLE(
    coll,
    name="SusOps.app",
    icon="assets/susops.icns",
    bundle_identifier="net.odt.susops",
    info_plist={
        "CFBundleShortVersionString": "3.0.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
```

- [ ] **Step 3: Run spec tests**

```bash
uv run pytest tests/test_packaging.py -v -k "pyinstaller or spec"
```

Expected: both PASS.

- [ ] **Step 4: Commit**

```bash
git add packaging/macos/
git commit -m "feat(macos): add PyInstaller entry script and susops.spec"
```

---

## Task 7: Write compute_resource_shas.py (TDD)

**Files:**
- Modify: `tests/conftest.py`
- Create: `scripts/compute_resource_shas.py`
- Create: `tests/test_scripts.py`

- [ ] **Step 1: Add scripts/ to sys.path in conftest.py**

Append to the existing `tests/conftest.py`:

```python
import sys
from pathlib import Path

_scripts_dir = str(Path(__file__).parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
```

- [ ] **Step 2: Write failing tests — create tests/test_scripts.py**

```python
# tests/test_scripts.py
"""Unit tests for packaging helper scripts (HTTP calls mocked)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_resp(body: bytes) -> MagicMock:
    m = MagicMock()
    m.read.return_value = body
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


def _pypi_body(name: str, version: str, sha256: str) -> bytes:
    return json.dumps({
        "urls": [{
            "packagetype": "sdist",
            "url": f"https://files.pythonhosted.org/packages/source/{name[0]}/{name}/{name}-{version}.tar.gz",
            "digests": {"sha256": sha256},
        }]
    }).encode()


# ── compute_resource_shas ─────────────────────────────────────────────────────

def test_get_pypi_sdist_returns_url_and_sha256():
    from compute_resource_shas import get_pypi_sdist

    fake_sha = "ab12" * 16
    with patch("urllib.request.urlopen", return_value=_mock_resp(_pypi_body("rich", "14.3.3", fake_sha))):
        result = get_pypi_sdist("rich", "14.3.3")

    assert result["sha256"] == fake_sha
    assert "rich-14.3.3.tar.gz" in result["url"]


def test_get_pypi_sdist_returns_none_when_no_sdist():
    from compute_resource_shas import get_pypi_sdist

    body = json.dumps({"urls": [{"packagetype": "bdist_wheel", "url": "x", "digests": {"sha256": "y"}}]}).encode()
    with patch("urllib.request.urlopen", return_value=_mock_resp(body)):
        assert get_pypi_sdist("pkg", "1.0") is None


def test_compute_excludes_dev_packages():
    from compute_resource_shas import compute_resource_shas

    pip_out = json.dumps([
        {"name": "susops", "version": "3.0.0"},
        {"name": "pytest", "version": "9.0.3"},
        {"name": "rich", "version": "14.3.3"},
    ]).encode()

    fake_sha = "dead" * 16
    mock_pip = MagicMock(stdout=pip_out.decode(), returncode=0)

    with patch("subprocess.run", return_value=mock_pip):
        with patch("urllib.request.urlopen", return_value=_mock_resp(_pypi_body("rich", "14.3.3", fake_sha))):
            result = compute_resource_shas()

    assert "rich" in result
    assert "susops" not in result
    assert "pytest" not in result
```

- [ ] **Step 3: Run to verify failure**

```bash
uv run pytest tests/test_scripts.py -v -k "compute"
```

Expected: `ModuleNotFoundError: No module named 'compute_resource_shas'`

- [ ] **Step 4: Create scripts/compute_resource_shas.py**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_scripts.py -v -k "compute"
```

Expected: all 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/compute_resource_shas.py tests/test_scripts.py tests/conftest.py
git commit -m "feat: add compute_resource_shas.py with tests"
```

---

## Task 8: Extend update_aur_pkgver.py (TDD)

**Files:**
- Modify: `scripts/update_aur_pkgver.py`
- Modify: `tests/test_scripts.py`

- [ ] **Step 1: Append failing tests to tests/test_scripts.py**

```python
# ── update_aur_pkgver ─────────────────────────────────────────────────────────

def test_update_pkgbuild_bumps_version(tmp_path):
    from update_aur_pkgver import update_pkgbuild
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgver=3.0.0\npkgrel=2\nsha256sums=('SKIP')\n")
    update_pkgbuild(pkgbuild, "3.1.0", "abc123")
    text = pkgbuild.read_text()
    assert "pkgver=3.1.0" in text
    assert "pkgrel=1" in text
    assert "sha256sums=('abc123')" in text


def test_update_pkgbuild_rejects_bad_version(tmp_path):
    from update_aur_pkgver import update_pkgbuild
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgver=3.0.0\npkgrel=1\nsha256sums=('SKIP')\n")
    with pytest.raises(ValueError, match="X.Y.Z"):
        update_pkgbuild(pkgbuild, "bad-version", "abc123")


def test_fetch_sha256_downloads_and_hashes():
    from update_aur_pkgver import fetch_sha256
    content = b"fake tarball bytes"
    expected = hashlib.sha256(content).hexdigest()
    with patch("urllib.request.urlopen", return_value=_mock_resp(content)):
        result = fetch_sha256("https://example.com/pkg.tar.gz")
    assert result == expected


def test_main_aur_patches_file(tmp_path, monkeypatch):
    from update_aur_pkgver import main
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgver=3.0.0\npkgrel=1\nsha256sums=('SKIP')\n")
    content = b"fake tarball"
    expected_sha = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr("sys.argv", ["script", "3.1.0"])
    monkeypatch.setattr("update_aur_pkgver.PKGBUILD", pkgbuild)
    with patch("urllib.request.urlopen", return_value=_mock_resp(content)):
        main()
    text = pkgbuild.read_text()
    assert "pkgver=3.1.0" in text
    assert f"sha256sums=('{expected_sha}')" in text
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_scripts.py -v -k "pkgbuild or aur or fetch_sha256"
```

Expected: `ImportError` — `update_pkgbuild`, `fetch_sha256` not yet exported.

- [ ] **Step 3: Rewrite scripts/update_aur_pkgver.py**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_scripts.py -v -k "pkgbuild or aur or fetch_sha256"
```

Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/update_aur_pkgver.py tests/test_scripts.py
git commit -m "feat: extend update_aur_pkgver.py with sha256 computation and tests"
```

---

## Task 9: Extend update_homebrew_sha.py (TDD)

**Files:**
- Modify: `scripts/update_homebrew_sha.py`
- Modify: `tests/test_scripts.py`

- [ ] **Step 1: Append failing tests to tests/test_scripts.py**

```python
# ── update_homebrew_sha ───────────────────────────────────────────────────────

def _formula_template() -> str:
    return (
        'class Susops < Formula\n'
        '  url "https://github.com/mashb1t/susops/archive/v3.0.0.tar.gz"\n'
        '  sha256 "PLACEHOLDER"\n\n'
        '  resource "rich" do\n'
        '    url "https://files.pythonhosted.org/packages/source/r/rich/rich-14.3.3.tar.gz"\n'
        '    sha256 "PLACEHOLDER"\n'
        '  end\n\n'
        '  resource "pydantic" do\n'
        '    url "https://files.pythonhosted.org/packages/source/p/pydantic/pydantic-2.12.5.tar.gz"\n'
        '    sha256 "PLACEHOLDER"\n'
        '  end\n'
        'end\n'
    )


def test_update_formula_main_sha(tmp_path):
    from update_homebrew_sha import update_formula_main_sha
    formula = tmp_path / "susops.rb"
    formula.write_text(_formula_template())
    update_formula_main_sha(formula, "3.1.0", "newsha256abc")
    text = formula.read_text()
    assert 'url "https://github.com/mashb1t/susops/archive/v3.1.0.tar.gz"' in text
    lines = text.splitlines()
    first_sha = next(l for l in lines if "sha256" in l)
    assert "newsha256abc" in first_sha


def test_update_formula_resource_shas(tmp_path):
    from update_homebrew_sha import update_formula_resource_shas
    formula = tmp_path / "susops.rb"
    formula.write_text(_formula_template())
    shas = {
        "rich":    {"sha256": "richsha123",    "url": "https://example.com/rich.tar.gz"},
        "pydantic": {"sha256": "pydanticsha456", "url": "https://example.com/pydantic.tar.gz"},
    }
    update_formula_resource_shas(formula, shas)
    text = formula.read_text()
    assert "richsha123" in text
    assert "pydanticsha456" in text
    assert "PLACEHOLDER" not in text


def test_update_cask_sha(tmp_path):
    from update_homebrew_sha import update_cask_sha
    cask = tmp_path / "susops.rb"
    cask.write_text(
        'cask "susops" do\n'
        '  version :latest\n'
        '  sha256 :no_check\n'
        '  url "https://github.com/mashb1t/susops/releases/latest/download/SusOps-#{version}-arm64.dmg"\n'
        'end\n'
    )
    update_cask_sha(cask, "3.1.0", "dmgsha789")
    text = cask.read_text()
    assert 'version "3.1.0"' in text
    assert 'sha256 "dmgsha789"' in text
    assert "SusOps-3.1.0-arm64.dmg" in text
```

- [ ] **Step 2: Run tests to verify failure**

```bash
uv run pytest tests/test_scripts.py -v -k "formula or cask"
```

Expected: `ImportError` — functions not yet defined.

- [ ] **Step 3: Rewrite scripts/update_homebrew_sha.py**

```python
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
import urllib.request
from pathlib import Path

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
    """Patch each resource block's sha256 (and url if provided)."""
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

    main_sha = sha256_of_url(_GITHUB_TARBALL.format(version=version))
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
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_scripts.py -v -k "formula or cask"
```

Expected: all 3 PASS.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/update_homebrew_sha.py tests/test_scripts.py
git commit -m "feat: extend update_homebrew_sha.py with resource/cask sha support and tests"
```

---

## Task 10: Populate formula sha256s with real values

**Files:**
- Modify: `packaging/homebrew/Formula/susops.rb`

- [ ] **Step 1: Run the script**

```bash
uv run python scripts/update_homebrew_sha.py 3.0.0
```

This downloads the GitHub v3.0.0 tarball sha256, queries PyPI for all resource sha256s, and patches `packaging/homebrew/Formula/susops.rb`.

If `v3.0.0` is not yet published on GitHub, compute the tarball sha locally:

```bash
# Alternative: compute sha from a local build
uv build
SHA=$(sha256sum dist/susops-3.0.0.tar.gz | cut -d' ' -f1)
sed -i "0,/sha256 \"PLACEHOLDER\"/s/sha256 \"PLACEHOLDER\"/sha256 \"$SHA\"/" \
    packaging/homebrew/Formula/susops.rb
uv run python scripts/compute_resource_shas.py > /tmp/shas.tsv
# then run update_formula_resource_shas programmatically:
python3 - <<EOF
from pathlib import Path
import sys
sys.path.insert(0, "scripts")
from update_homebrew_sha import update_formula_resource_shas, FORMULA
shas = {}
for line in Path("/tmp/shas.tsv").read_text().splitlines():
    name, version, sha256, url = line.split("\t")
    shas[name] = {"sha256": sha256, "url": url}
update_formula_resource_shas(FORMULA, shas)
EOF
```

- [ ] **Step 2: Verify no PLACEHOLDERs remain**

```bash
grep PLACEHOLDER packaging/homebrew/Formula/susops.rb && echo "FAIL: PLACEHOLDERs remain" || echo "OK"
```

Expected: `OK`

- [ ] **Step 3: Run all packaging tests**

```bash
uv run pytest tests/test_packaging.py -v
```

Expected: all tests PASS including `test_formula_has_no_placeholder`.

- [ ] **Step 4: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add packaging/homebrew/Formula/susops.rb
git commit -m "feat(homebrew): populate formula sha256s for current release"
```

---

## Task 11: Refactor release.yml — split jobs + add build-dmg

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Replace .github/workflows/release.yml entirely**

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

permissions:
  contents: write

env:
  VERSION: ${{ github.ref_name }}

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
      - run: uv python install 3.12
      - run: uv sync --extra dev --extra share
      - run: uv run pytest -x -q

  build-pypi:
    needs: [test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
      - run: uv python install 3.12
      - run: uv build
      - uses: softprops/action-gh-release@v2
        with:
          files: dist/*
          generate_release_notes: true
      - run: uv publish
        env:
          UV_PUBLISH_TOKEN: ${{ secrets.PYPI_TOKEN }}

  build-dmg:
    needs: [test]
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install deps
        run: pip install pyinstaller rumps pyobjc && pip install -e ".[tray-mac]"
      - name: Generate susops.icns
        run: |
          mkdir -p SusOps.iconset
          sips -z 16   16   assets/icon.png --out SusOps.iconset/icon_16x16.png
          sips -z 32   32   assets/icon.png --out SusOps.iconset/icon_16x16@2x.png
          sips -z 32   32   assets/icon.png --out SusOps.iconset/icon_32x32.png
          sips -z 64   64   assets/icon.png --out SusOps.iconset/icon_32x32@2x.png
          sips -z 128  128  assets/icon.png --out SusOps.iconset/icon_128x128.png
          sips -z 256  256  assets/icon.png --out SusOps.iconset/icon_128x128@2x.png
          sips -z 256  256  assets/icon.png --out SusOps.iconset/icon_256x256.png
          sips -z 512  512  assets/icon.png --out SusOps.iconset/icon_256x256@2x.png
          sips -z 512  512  assets/icon.png --out SusOps.iconset/icon_512x512.png
          iconutil -c icns SusOps.iconset -o assets/susops.icns
      - name: Build SusOps.app
        run: pyinstaller packaging/macos/susops.spec --clean --noconfirm
      - name: Create dmg
        run: |
          VER="${VERSION#v}"
          hdiutil create -volname "SusOps" -srcfolder dist/SusOps.app -ov -format UDZO "SusOps-${VER}-arm64.dmg"
        env:
          VERSION: ${{ env.VERSION }}
      - name: Upload dmg to release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          VERSION: ${{ env.VERSION }}
        run: |
          VER="${VERSION#v}"
          for i in $(seq 1 12); do gh release view "$VERSION" && break || sleep 10; done
          gh release upload "$VERSION" "SusOps-${VER}-arm64.dmg" --clobber
      - uses: actions/upload-artifact@v4
        with:
          name: dmg
          path: "SusOps-*-arm64.dmg"
          retention-days: 1
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: split release job, add build-dmg for macOS arm64"
```

---

## Task 12: Add update-tap CI job

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Append update-tap job to release.yml**

Add inside the `jobs:` block (after `build-dmg`):

```yaml
  update-tap:
    needs: [build-pypi, build-dmg]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: main
      - uses: actions/checkout@v4
        with:
          repository: mashb1t/homebrew-susops
          token: ${{ secrets.HOMEBREW_TAP_TOKEN }}
          path: tap
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/download-artifact@v4
        with:
          name: dmg
          path: main
      - name: Update formula and cask sha256s
        working-directory: main
        env:
          VERSION: ${{ env.VERSION }}
        run: |
          VER="${VERSION#v}"
          pip install -e ".[share]"
          python scripts/update_homebrew_sha.py "${VER}" --dmg "SusOps-${VER}-arm64.dmg"
      - name: Copy to tap
        run: |
          cp main/packaging/homebrew/Formula/susops.rb tap/Formula/susops.rb
          cp main/packaging/homebrew/Casks/susops.rb  tap/Casks/susops.rb
      - name: Commit and push
        working-directory: tap
        env:
          VERSION: ${{ env.VERSION }}
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add Formula/susops.rb Casks/susops.rb
          git commit -m "chore: bump to ${VERSION}"
          git push
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: add update-tap job to push Formula and Cask to homebrew-susops"
```

---

## Task 13: Add update-aur CI job

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Append update-aur job to release.yml**

Add inside the `jobs:` block (after `update-tap`):

```yaml
  update-aur:
    needs: [build-pypi]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Update PKGBUILD
        env:
          VERSION: ${{ env.VERSION }}
        run: |
          VER="${VERSION#v}"
          python scripts/update_aur_pkgver.py "${VER}"
      - name: Generate .SRCINFO via Arch Docker
        run: |
          docker run --rm \
            -v "${{ github.workspace }}/packaging/aur:/pkg" \
            archlinux:base-devel \
            sh -c "useradd -m builder && chown builder /pkg && su builder -c 'cd /pkg && makepkg --printsrcinfo > .SRCINFO'"
      - uses: webfactory/ssh-agent@v0.9.0
        with:
          ssh-private-key: ${{ secrets.AUR_SSH_KEY }}
      - name: Add AUR host key
        run: ssh-keyscan -t ed25519 aur.archlinux.org >> ~/.ssh/known_hosts
      - name: Clone and push AUR repo
        env:
          VERSION: ${{ env.VERSION }}
        run: |
          git clone ssh://aur@aur.archlinux.org/susops.git aur-repo
          cp packaging/aur/PKGBUILD  aur-repo/PKGBUILD
          cp packaging/aur/.SRCINFO  aur-repo/.SRCINFO
          cd aur-repo
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add PKGBUILD .SRCINFO
          git commit -m "chore: bump to ${VERSION}"
          git push
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: add update-aur job to push PKGBUILD + .SRCINFO to AUR"
```

- [ ] **Step 3: Run final full test suite**

```bash
uv run pytest -x -q
```

Expected: all tests PASS.

- [ ] **Step 4: Run all new tests explicitly**

```bash
uv run pytest tests/test_packaging.py tests/test_scripts.py tests/test_version.py -v
```

Expected: all PASS.

---

## Required GitHub Secrets

Add these in **Settings → Secrets and variables → Actions** before the first release:

| Secret | Description | How to obtain |
|---|---|---|
| `PYPI_TOKEN` | PyPI API token (already exists) | pypi.org → Account → API tokens |
| `HOMEBREW_TAP_TOKEN` | GitHub PAT with `repo` scope on `homebrew-susops` | GitHub → Settings → Developer settings → Personal access tokens (classic) |
| `AUR_SSH_KEY` | SSH private key registered with AUR account | `ssh-keygen -t ed25519 -C "github-actions"`, register public key at aur.archlinux.org |
