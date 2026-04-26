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
