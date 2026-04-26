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
