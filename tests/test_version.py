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

def test_version_fallback_reads_pyproject(monkeypatch):
    """Verify the tomllib fallback works when the package is not installed."""
    import importlib.metadata as _meta
    import importlib
    import susops.version as _ver

    def _raise(_pkg):
        raise _meta.PackageNotFoundError(_pkg)

    monkeypatch.setattr(_meta, "version", _raise)
    importlib.reload(_ver)

    import tomllib
    from pathlib import Path
    expected = tomllib.loads(
        (Path(_ver.__file__).parent.parent.parent / "pyproject.toml").read_text()
    )["project"]["version"]
    assert _ver.VERSION == expected
