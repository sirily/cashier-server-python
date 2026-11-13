"""Read release metadata without importing or executing application code."""

import ast
import re
import sys
import tomllib
from pathlib import Path

VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


def package_version(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [p for p in lock["package"] if p["name"] == "cashier-server"]
    if len(packages) != 1 or packages[0].get("source") != {"editable": "."}:
        raise ValueError("uv.lock must contain exactly one editable cashier-server root package")
    module = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    app_calls = [
        node for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastAPI"
    ]
    if len(app_calls) != 1:
        raise ValueError("main.py must declare exactly one FastAPI application")
    api_versions = [ast.literal_eval(k.value) for k in app_calls[0].keywords if k.arg == "version"]
    version = project["project"]["version"]
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError(f"Invalid release version: {version}")
    if api_versions != [version] or packages[0]["version"] != version:
        raise ValueError("pyproject.toml, uv.lock root package and FastAPI versions must match")
    return version


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: release_version.py REPOSITORY_ROOT")
        print(package_version(Path(sys.argv[1])))
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        sys.exit(str(exc))
