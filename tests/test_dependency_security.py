"""Prevent regression to versions covered by the November 2026 dependency audit.

These offline checks complement, but do not replace, a live vulnerability audit.
"""

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
SECURITY_FLOORS = {
    "fastapi": "0.163.1",
    "starlette": "1.3.1",
    "anyio": "4.14.2",
    "certifi": "2026.10.19",
    "idna": "3.15",
    "pygments": "2.20.0",
    "pytest": "9.0.3",
    "soupsieve": "4.0",
    "werkzeug": "3.1.9",
}


@pytest.mark.parametrize("name,minimum", SECURITY_FLOORS.items())
def test_locked_and_installed_dependencies_meet_security_floor(name, minimum):
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = [package for package in lock["package"] if package["name"] == name]
    assert packages
    assert all(Version(package["version"]) >= Version(minimum) for package in packages)
    assert Version(version(name)) >= Version(minimum)


@pytest.mark.parametrize("name,minimum", SECURITY_FLOORS.items())
def test_resolution_preserves_security_floor(name, minimum):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = [
        *project["project"]["dependencies"],
        *project["dependency-groups"]["dev"],
        *project["tool"]["uv"]["constraint-dependencies"],
    ]
    requirement = next(Requirement(value) for value in requirements if Requirement(value).name == name)
    assert f">={minimum}" in {str(specifier) for specifier in requirement.specifier}


def test_production_image_pins_base_and_installer_without_retaining_uv_cache():
    dockerfile = (ROOT / "Dockerfile").read_text()
    dockerfile = re.sub(r"\\\n\s*", " ", dockerfile)
    dockerfile = re.sub(r"^\s*#.*$", "", dockerfile, flags=re.MULTILINE)
    stages = re.findall(r"^FROM ([^\n]+)\n(.*?)(?=^FROM |\Z)", dockerfile, re.MULTILINE | re.DOTALL)
    assert len(stages) == 3, "Expected separate base, builder and runtime stages"
    assert re.fullmatch(
        r"python:3\.12\.16-slim-trixie@sha256:[0-9a-f]{64} AS base",
        stages[0][0],
    )
    assert [header for header, _ in stages[1:]] == ["base AS builder", "base AS runtime"]
    base, builder, runtime = [body for _, body in stages]

    assert "PYTHONDONTWRITEBYTECODE=1" in base
    assert "PYTHONUNBUFFERED=1" in base
    assert 'PATH="/app/.venv/bin:$PATH"' in base
    assert re.search(r"^WORKDIR /app$", base, re.MULTILINE)
    assert "apt-get update" in base
    assert "apt-get upgrade -y --no-install-recommends" in base
    assert "apt-get install -y --no-install-recommends curl ca-certificates" in base
    assert "rm -rf /var/lib/apt/lists/*" in base
    assert not re.search(r"^(?:COPY|ADD)\b", base, re.MULTILINE)

    for setting in ("UV_COMPILE_BYTECODE=1", "UV_LINK_MODE=copy", "UV_PYTHON_DOWNLOADS=never"):
        assert setting in builder
        assert setting not in base + runtime
    assert re.search(r"apt-get install\b[^&\n]*\bgit\b", builder)
    assert "rm -rf /var/lib/apt/lists/*" in builder
    assert "python -m pip install --no-cache-dir --upgrade pip==26.3 uv==0.13.8" in builder
    assert "python -m pip uninstall --yes pip" in builder
    sync = next(line for line in builder.splitlines() if line.startswith("RUN uv sync "))
    assert {"--locked", "--no-dev", "--no-cache", "--no-editable"} <= set(sync.split())

    assert not re.search(r"\b(?:git|uv|uvx)\b", base + runtime)
    assert "pip install" not in base + runtime
    assert "python -m pip uninstall --yes pip" in runtime
    copies = re.findall(r"^(?:COPY|ADD)\b[^\n]*", runtime, re.MULTILINE)
    assert copies == ["COPY --from=builder /app/.venv /app/.venv"]
    assert "groupadd --system app" in runtime
    assert re.search(r"\buseradd\b[^&\n]*--system\b[^&\n]*\bapp\b", runtime)
    assert re.search(r"^USER app$", runtime, re.MULTILINE)
    assert "ENV BEANCOUNT_FILE=/workspace/main.bean" in runtime
    assert re.search(r"^EXPOSE 3000$", runtime, re.MULTILINE)
    assert re.search(r"^HEALTHCHECK\b[^\n]*\bCMD curl -fsS http://127\.0\.0\.1:3000/health\b", runtime, re.MULTILINE)
    assert 'CMD ["cashier-server"]' in runtime
    assert not re.search(r"^VOLUME\b", dockerfile, re.MULTILINE)
