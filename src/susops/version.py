"""Single source of truth for the susops package version."""
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

# In dev/editable environments, installed metadata can be stale after a local
# version bump. Prefer the repo pyproject when present.
_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
if _pyproject.exists():
    VERSION = tomllib.loads(_pyproject.read_text())["project"]["version"]
else:
    try:
        VERSION = version("susops")
    except PackageNotFoundError:
        VERSION = "0.0.0"
