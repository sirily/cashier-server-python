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
    assert re.search(
        r"^FROM python:3\.12\.\d+-slim-trixie@sha256:[0-9a-f]{64}$",
        dockerfile,
        re.MULTILINE,
    )
    assert "apt-get upgrade -y --no-install-recommends" in dockerfile
    assert "python -m pip install --no-cache-dir --upgrade pip==26.3 uv==0.13.8" in dockerfile
    assert "python -m pip uninstall --yes pip" in dockerfile
    sync = next(line for line in dockerfile.splitlines() if line.startswith("RUN uv sync "))
    assert {"--locked", "--no-dev", "--no-cache"} <= set(sync.split())
    assert "USER app" in dockerfile
    assert not re.search(r"^VOLUME\b", dockerfile, re.MULTILINE)
