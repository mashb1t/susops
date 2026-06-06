#!/usr/bin/env bash
# Build susops locally for 1:1 testing against the production
# distribution paths. Mirrors what release.yml does in CI but without
# any upload steps. Outputs land in dist/.
#
# Usage:
#   scripts/build-local.sh pypi             build wheel + sdist
#   scripts/build-local.sh brew             build .app + .dmg
#   scripts/build-local.sh all              both of the above
#   scripts/build-local.sh install-pypi     create a fresh venv and install the wheel into it
#   scripts/build-local.sh install-brew     copy SusOps.app to /Applications and clear quarantine
#   scripts/build-local.sh clean            wipe dist/, build/, *.egg-info, SusOps.iconset
#
# Honors UV (path to uv binary). Defaults to ~/.local/bin/uv then PATH.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

UV="${UV:-$HOME/.local/bin/uv}"
[ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
[ -n "$UV" ] || { echo "uv not found; set UV= or install astral.sh/uv" >&2; exit 1; }

VENV=".venv"
PYTHON="$VENV/bin/python"
PY_TARGET="3.14"

log() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }

ensure_venv() {
    if [ ! -x "$PYTHON" ]; then
        log "creating .venv with Python $PY_TARGET"
        "$UV" venv --python "$PY_TARGET" "$VENV"
    fi
}

cmd_pypi() {
    ensure_venv
    log "regenerating OpenAPI spec"
    "$UV" pip install --python "$PYTHON" --quiet -e ".[dev,tui]"
    "$PYTHON" tools/gen_openapi.py
    log "building wheel + sdist (output: dist/)"
    rm -rf dist
    "$UV" build
    ls -lh dist/
}

cmd_brew() {
    if [ "$(uname)" != "Darwin" ]; then
        echo "brew build is macOS-only" >&2; exit 1
    fi
    ensure_venv
    log "installing pyinstaller + tray-mac deps"
    "$UV" pip install --python "$PYTHON" --quiet pyinstaller rumps pyobjc
    "$UV" pip install --python "$PYTHON" --quiet -e ".[tray-mac]"
    log "building SusOps.app via PyInstaller"
    rm -rf dist/SusOps* build/SusOps*
    "$PYTHON" -m PyInstaller packaging/macos/susops.spec --clean --noconfirm
    local ver
    ver="$("$PYTHON" -c 'from susops import __version__ as v; print(v)')"
    local dmg="dist/SusOps-${ver}-arm64.dmg"
    log "creating $dmg"
    hdiutil create -volname "SusOps" -srcfolder dist/SusOps.app -ov -format UDZO "$dmg" >/dev/null
    ls -lh "$dmg"
}

cmd_install_pypi() {
    local wheel
    wheel="$(ls -t dist/susops-*.whl 2>/dev/null | head -1 || true)"
    [ -n "$wheel" ] || { echo "no wheel in dist/ — run 'pypi' first" >&2; exit 1; }
    local test_venv="/tmp/susops-pypi-test"
    rm -rf "$test_venv"
    log "creating throwaway venv at $test_venv"
    "$UV" venv --python "$PY_TARGET" "$test_venv"
    log "installing $wheel"
    "$UV" pip install --python "$test_venv/bin/python" --quiet "$wheel[tui]"
    log "smoke: susops ps"
    "$test_venv/bin/susops" ps || true
    log "binary lives at: $test_venv/bin/susops"
}

cmd_install_brew() {
    if [ "$(uname)" != "Darwin" ]; then
        echo "brew install is macOS-only" >&2; exit 1
    fi
    [ -d dist/SusOps.app ] || { echo "no dist/SusOps.app — run 'brew' first" >&2; exit 1; }
    log "killing any running SusOps + daemon"
    pkill -9 -f 'SusOps.app|services_daemon' 2>/dev/null || true
    log "removing /Applications/SusOps.app"
    rm -rf /Applications/SusOps.app
    log "copying fresh build to /Applications/"
    cp -R dist/SusOps.app /Applications/
    xattr -dr com.apple.quarantine /Applications/SusOps.app 2>/dev/null || true
    log "launch with: open -a SusOps"
}

cmd_all() { cmd_pypi; cmd_brew; }

cmd_clean() {
    log "removing dist/ build/ *.egg-info SusOps.iconset"
    rm -rf dist build src/*.egg-info SusOps.iconset
}

case "${1:-}" in
    pypi) cmd_pypi ;;
    brew) cmd_brew ;;
    all) cmd_all ;;
    install-pypi) cmd_install_pypi ;;
    install-brew) cmd_install_brew ;;
    clean) cmd_clean ;;
    *)
        sed -n '/^# Usage/,/^# Honors/p' "$0" | sed 's/^# //; s/^#//'
        exit 1
        ;;
esac
