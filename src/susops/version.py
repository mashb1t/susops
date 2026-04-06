from pathlib import Path
import ast

_root = Path(__file__).parent.parent.parent
_version_file = _root / "version.py"
_tree = ast.parse(_version_file.read_text())
VERSION = next(
    node.value.s
    for node in ast.walk(_tree)
    if isinstance(node, ast.Assign)
    and any(t.id == "VERSION" for t in node.targets if isinstance(t, ast.Name))
)
