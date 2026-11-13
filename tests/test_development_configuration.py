"""Keep editor launchers and release metadata consistent without starting tools."""

import ast
import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_server_versions_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    root_package = next(p for p in lock["package"] if p["name"] == "cashier-server")
    module = ast.parse((ROOT / "main.py").read_text())
    app_call = next(
        node for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastAPI"
    )
    api_version = next(ast.literal_eval(k.value) for k in app_call.keywords if k.arg == "version")

    assert root_package["source"] == {"editable": "."}
    assert project["project"]["version"] == root_package["version"] == api_version


def test_typechecker_uses_project_virtual_environment():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    settings = project["tool"]["pyright"]

    assert settings["venvPath"] == "."
    assert settings["venv"] == ".venv"
    assert settings["typeCheckingMode"] == "standard"


@pytest.mark.parametrize("name", ["serena", "gitnexus"])
def test_mcp_and_zed_launch_the_same_pinned_tool(name):
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"][name]
    zed = json.loads((ROOT / ".zed/settings.json").read_text())["context_servers"][name]

    assert mcp["type"] == "stdio"
    assert mcp["command"] == zed["command"]
    assert mcp["args"] == zed["args"]
    assert not Path(zed["command"]).is_absolute()
    assert "remote" not in zed
    if name == "serena":
        assert "serena-agent==2.1.0" in mcp["args"]
        assert "--project-from-cwd" in mcp["args"]
    else:
        assert "--package=gitnexus@1.6.12" in mcp["args"]
        assert mcp["args"][-1] == "mcp"


def test_code_intelligence_is_not_packaged_as_server_code():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    assert not any("serena" in dep or "gitnexus" in dep for dep in dependencies)
    wheel_files = project["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    assert set(wheel_files) == {"main.py", "writeback.py", "cashier_snapshot"}
    docker_ignores = (ROOT / ".dockerignore").read_text().splitlines()
    assert {".serena", ".gitnexus", ".zed", ".mcp.json"} <= set(docker_ignores)
    git_ignores = (ROOT / ".gitignore").read_text().splitlines()
    assert {".gitnexus/", ".serena/logs/", ".serena/memories/"} <= set(git_ignores)
    graph_ignores = (ROOT / ".gitnexusignore").read_text().splitlines()
    assert not any(line.startswith("!") for line in graph_ignores)
