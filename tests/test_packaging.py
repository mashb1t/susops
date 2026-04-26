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
