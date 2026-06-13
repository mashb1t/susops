"""Regression tests for update_cask_sha regex idempotency."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from update_homebrew_sha import update_cask_sha  # noqa: E402

PINNED_CASK = """\
cask "susops" do
  version "0.0.0"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  url "https://github.com/mashb1t/susops/releases/download/v#{version}/SusOps-#{version}-arm64.dmg"
  app "SusOps.app"
end
"""

LEGACY_CASK = """\
cask "susops" do
  version :latest
  sha256 :no_check

  url "https://github.com/mashb1t/susops/releases/latest/download/SusOps-#{version}-arm64.dmg"
  app "SusOps.app"
end
"""


def test_cask_update_rewrites_pinned_values(tmp_path: Path) -> None:
    """Subsequent releases must overwrite an already-pinned version + sha256."""
    p = tmp_path / "susops.rb"
    p.write_text(PINNED_CASK)

    update_cask_sha(p, "3.1.0", "a" * 64)
    out = p.read_text()
    assert 'version "3.1.0"' in out
    assert f'sha256 "{"a" * 64}"' in out

    update_cask_sha(p, "3.2.0", "b" * 64)
    out = p.read_text()
    assert 'version "3.2.0"' in out
    assert f'sha256 "{"b" * 64}"' in out
    assert 'version "3.1.0"' not in out


def test_cask_update_handles_legacy_initial_state(tmp_path: Path) -> None:
    """A Cask still using ``:latest`` / ``:no_check`` must pin cleanly."""
    p = tmp_path / "susops.rb"
    p.write_text(LEGACY_CASK)

    update_cask_sha(p, "3.0.0-rc6.dev1", "c" * 64)
    out = p.read_text()
    assert 'version "3.0.0-rc6.dev1"' in out
    assert f'sha256 "{"c" * 64}"' in out
    assert '#{version}' in out


def test_cask_update_only_touches_first_occurrence(tmp_path: Path) -> None:
    """Only the first version/sha256 is replaced, even if the file has more."""
    p = tmp_path / "susops.rb"
    p.write_text(
        'cask "susops" do\n'
        '  version "0.0.0"\n'
        '  sha256 "0000000000000000000000000000000000000000000000000000000000000000"\n'
        '  # historical: sha256 "deadbeef"\n'
        'end\n'
    )

    update_cask_sha(p, "9.9.9", "9" * 64)
    out = p.read_text()
    assert 'version "9.9.9"' in out
    assert f'sha256 "{"9" * 64}"' in out
    # The comment line must survive unchanged.
    assert '# historical: sha256 "deadbeef"' in out
