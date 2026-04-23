# Packaging Design: Homebrew + AUR Distribution

**Date:** 2026-04-23  
**Status:** Approved  
**Scope:** Automate macOS (Homebrew Formula + Cask) and Arch Linux (AUR) packaging on every tagged GitHub release.

---

## 1. Architecture Overview

```
git tag v3.1.0  →  push to GitHub
                       │
                       ▼
          .github/workflows/release.yml
                       │
         ┌─────────────┼──────────────┐
         ▼             ▼              ▼
      [test]      [build-pypi]   [build-dmg]
      ubuntu       ubuntu         macos-latest (arm64)
                   │              │
                   │  PyPI        │  PyInstaller → SusOps.app
                   │  wheel+sdist │  hdiutil    → SusOps-{v}-arm64.dmg
                   │              │  gh release upload
                   │              │
                   └──────┬───────┘
                          │  (both must succeed)
               ┌──────────┴──────────┐
               ▼                     ▼
         [update-tap]          [update-aur]
          ubuntu                ubuntu
          PAT secret            AUR SSH key secret
          clones homebrew-susops clones aur.archlinux.org/susops.git
          patches Formula+Cask  patches PKGBUILD + .SRCINFO
          commits + pushes      commits + pushes
```

**Single version source of truth:** `pyproject.toml`. The git tag (`v3.1.0`) drives all version substitutions in packaging scripts and CI.

**`version.py` migration (required before removal):** Currently `src/susops/version.py` reads root `version.py` via AST, and `src/susops/__init__.py` imports `VERSION` from it. Removing root `version.py` without migration breaks runtime. Migration: replace `src/susops/version.py` with `importlib.metadata.version("susops")` — this reads the version from the installed package metadata, eliminating the AST-file dependency. Root `version.py` is then removed.

---

## 2. Release Assets

Every tagged release produces these artifacts attached to the GitHub release:

| Asset | Produced by | How referenced |
|---|---|---|
| `susops-{v}.tar.gz` (sdist) | `build-pypi` via `uv build` | PyPI |
| `susops-{v}-py3-none-any.whl` | `build-pypi` via `uv build` | PyPI |
| `SusOps-{v}-arm64.dmg` | `build-dmg` via PyInstaller + hdiutil | Homebrew Cask |
| source `.zip` / `.tar.gz` | GitHub auto-generated | AUR PKGBUILD, Homebrew Formula |

---

## 3. Packaging Artifacts

### 3.1 `packaging/aur/PKGBUILD`

**Changes from current state:**

- `depends`: remove GTK/tray packages; add `python-ruamel-yaml` (AUR). GTK packages move to `optdepends`.
- `optdepends`: add `python-gobject`, `gtk3`, `libayatana-appindicator` (tray app); `python-textual`, `python-textual-plotext` (TUI); `python-cryptography`, `python-aiohttp` (file sharing); `socat` (UDP forwarding).
- `sha256sums`: CI fills in the computed sha256 of the GitHub source tarball (no more `SKIP`).
- `makedepends`: replace `python-build python-installer python-wheel python-setuptools` with `python-build python-installer python-wheel` (setuptools not needed with pyproject.toml builds).

### 3.2 `packaging/aur/susops-tray.desktop` (new file)

Desktop entry for the GTK tray app. Required by the PKGBUILD `install` step.

```ini
[Desktop Entry]
Name=SusOps Tray
Comment=SSH SOCKS5 proxy manager — system tray
Exec=susops-tray
Icon=susops
Type=Application
Categories=Network;Utility;
```

### 3.3 `packaging/homebrew/Formula/susops.rb`

**Changes from current state:**

- All resource `sha256` values computed and filled in (no more `PLACEHOLDER`).
- Resource versions pinned to exact versions matching `pyproject.toml` minimum bounds.
- Add `livecheck` block pointing to GitHub tags.
- Add `textual-plotext` as a resource (currently missing).
- Keep `socat` as a system `depends_on`.

### 3.4 `packaging/homebrew/Casks/susops.rb`

**Changes from current state:**

- URL points to `SusOps-{version}-arm64.dmg` release asset.
- sha256 filled in by CI after `.dmg` is uploaded.

### 3.5 `packaging/macos/susops.spec` (new file)

PyInstaller spec for the macOS tray app:

- Entry point: `susops.tray` (`susops-tray` console script)
- App name: `SusOps`
- Icon: `assets/susops.icns`
- Bundle identifier: `net.odt.susops`
- `windowed=True` (no terminal window; tray-only app)
- Hidden imports: `objc`, `Foundation`, `AppKit`, `rumps` — required because PyInstaller cannot detect these ObjC-bridged imports statically. Expect iteration on this list during implementation.
- `collect_all("rumps")` to bundle all rumps data files.

### 3.6 `assets/susops.icns` (new file, manual prerequisite)

macOS app icon. Must be created manually once before the first `build-dmg` CI run: convert existing PNG assets using `iconutil` or `sips`. This is a one-time step, not automated.

---

## 4. Scripts

All scripts live in `scripts/` and are usable locally without CI. They operate on relative paths from the repo root.

### 4.1 `scripts/update_aur_pkgver.py` (extended)

**Current:** bumps `pkgver` and resets `pkgrel=1`.  
**After:** also fetches the GitHub tarball, computes sha256, patches `sha256sums=('...')`.

```
Usage: python scripts/update_aur_pkgver.py 3.1.0
```

### 4.2 `scripts/update_homebrew_sha.py` (extended)

**Current:** patches main tarball sha256 only.  
**After:** also patches all resource sha256s (downloads each PyPI package via `pip download`, computes sha256) and patches the Cask sha256 (requires the `.dmg` URL or local path as an optional argument).

```
Usage: python scripts/update_homebrew_sha.py 3.1.0
       python scripts/update_homebrew_sha.py 3.1.0 --dmg SusOps-3.1.0-arm64.dmg
```

### 4.3 `scripts/compute_resource_shas.py` (new)

Standalone helper: given a version, prints `name → sha256` for every PyPI package that must appear as a `resource` block in the Homebrew formula.

**Homebrew requires every transitive dependency** as a resource block — `virtualenv_install_with_resources` installs them all into an isolated venv. Direct deps (pydantic, ruamel.yaml, psutil, textual, textual-plotext, cryptography, aiohttp) each pull transitive deps: pydantic → `pydantic-core`, `annotated-types`, `typing-extensions`; textual → `rich`, `markdown-it-py`, `mdurl`, `platformdirs`; aiohttp → `multidict`, `yarl`, `frozenlist`, `aiosignal`, `attrs`; etc. The real list is ~30–50 packages depending on the extras installed.

The script resolves this by running:

```
pip download --no-binary :all: susops[tui,share] \
  --dest /tmp/susops-resources/
```

then computing sha256 for each downloaded sdist. This produces the complete, correct resource list for the current release.

```
Usage: python scripts/compute_resource_shas.py 3.1.0
```

**Formula class header note:** The formula must include `include Language::Python::Virtualenv` inside the class definition for `virtualenv_install_with_resources` to work.

---

## 5. CI Pipeline Details

### 5.1 `build-dmg` job

```yaml
runs-on: macos-latest
needs: [test]
steps:
  - checkout
  - install python + uv
  - uv sync --extra tray-mac
  - pip install pyinstaller
  - pyinstaller packaging/macos/susops.spec --clean --noconfirm
  - hdiutil create -volname SusOps -srcfolder dist/SusOps.app
      -ov -format UDZO SusOps-{version}-arm64.dmg
  - gh release upload v{version} SusOps-{version}-arm64.dmg --clobber
```

### 5.2 `update-tap` job

```yaml
runs-on: ubuntu-latest
needs: [build-pypi, build-dmg]
env:
  HOMEBREW_TAP_TOKEN: ${{ secrets.HOMEBREW_TAP_TOKEN }}
steps:
  - checkout main repo (for scripts)
  - checkout homebrew-susops via PAT into ./tap/
  - python scripts/update_homebrew_sha.py {version} --dmg-from-release
      (downloads .dmg from release, computes sha256, patches cask)
  - cp packaging/homebrew/Formula/susops.rb tap/Formula/
  - cp packaging/homebrew/Casks/susops.rb   tap/Casks/
  - git -C tap commit -am "chore: bump to v{version}"
  - git -C tap push
```

### 5.3 `update-aur` job

```yaml
runs-on: ubuntu-latest
needs: [build-pypi]
env:
  AUR_SSH_KEY: ${{ secrets.AUR_SSH_KEY }}
steps:
  - checkout main repo (for scripts + PKGBUILD)
  - setup SSH agent with AUR_SSH_KEY
  - add aur.archlinux.org to known_hosts
  - git clone ssh://aur@aur.archlinux.org/susops.git ./aur-repo/
  - python scripts/update_aur_pkgver.py {version}
      (patches PKGBUILD pkgver + sha256sums)
  - cp packaging/aur/PKGBUILD        ./aur-repo/
  - cp packaging/aur/susops-tray.desktop  (for reference, not committed to AUR)
  - docker run --rm -v ./aur-repo:/pkg archlinux:base-devel
      sh -c "cd /pkg && makepkg --printsrcinfo > .SRCINFO"
  - git -C aur-repo add PKGBUILD .SRCINFO
  - git -C aur-repo commit -m "chore: bump to v{version}"
  - git -C aur-repo push
```

**Note:** `makepkg --printsrcinfo` requires an Arch Linux environment. CI uses the official `archlinux:base-devel` Docker image for this step only, avoiding a full Arch runner.

---

## 6. Testing

### 6.1 `tests/test_scripts.py` (new)

Unit tests with mocked HTTP for the helper scripts:

- `test_update_aur_pkgver_bumps_version` — correct pkgver written, pkgrel reset to 1
- `test_update_aur_pkgver_rejects_bad_version` — non-semver input exits with error
- `test_update_aur_pkgver_patches_sha256` — sha256sums line updated correctly (mocked download)
- `test_update_homebrew_sha_patches_url` — url line updated to new version
- `test_update_homebrew_sha_patches_main_sha256` — first sha256 line updated (mocked download)
- `test_update_homebrew_sha_patches_resource_shas` — all resource sha256 lines updated (mocked pip download)
- `test_compute_resource_shas_output` — correct name→sha pairs for mocked response

### 6.2 `tests/test_packaging.py` (new)

Smoke tests that parse the packaging files and assert structural correctness:

- `test_pkgbuild_has_no_skip_sha` — `sha256sums=('SKIP')` not present
- `test_pkgbuild_depends_no_gtk` — GTK packages not in `depends` (only in `optdepends`)
- `test_pkgbuild_depends_ruamel` — `python-ruamel-yaml` present in `depends`
- `test_formula_has_no_placeholder` — no `PLACEHOLDER` string in formula
- `test_formula_has_livecheck` — `livecheck` block present
- `test_cask_has_no_placeholder` — no `PLACEHOLDER` string in cask
- `test_cask_url_has_arm64` — cask URL contains `arm64`
- `test_desktop_file_exists` — `packaging/aur/susops-tray.desktop` present
- `test_pyinstaller_spec_exists` — `packaging/macos/susops.spec` present
- `test_version_consistency` — pkgver in PKGBUILD matches version in Formula matches `pyproject.toml`

### 6.3 Existing CI

The `test.yml` workflow runs `pytest` on push/PR — `test_scripts.py` and `test_packaging.py` are picked up automatically.

---

## 7. Required GitHub Secrets

| Secret | Used by | Description |
|---|---|---|
| `PYPI_TOKEN` | `build-pypi` | PyPI publish token (existing) |
| `HOMEBREW_TAP_TOKEN` | `update-tap` | GitHub PAT with `repo` scope on `homebrew-susops` |
| `AUR_SSH_KEY` | `update-aur` | SSH private key for AUR account |

---

## 8. Out of Scope

- Intel (x86_64) `.dmg` build — add a second `build-dmg` matrix entry later if demand arises
- Homebrew-core submission — personal tap only
- `.SRCINFO` committed to this repo — it lives only in the AUR git repo
- Windows packaging
