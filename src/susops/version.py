"""Single source of truth for the susops package version."""
from importlib.metadata import version, PackageNotFoundError

try:
    VERSION = version("susops")
except PackageNotFoundError:
    # Running from source without installation — fall back to pyproject.toml
    import tomllib
    from pathlib import Path
    _pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"
    VERSION = tomllib.loads(_pyproject.read_text())["project"]["version"]
