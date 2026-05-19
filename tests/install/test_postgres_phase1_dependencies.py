"""Phase 1 dependency contract tests for PostgreSQL support."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _version_tuple(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    return tuple(int(part) for part in parts[:3])  # type: ignore[return-value]


def test_pyproject_declares_psycopg_binary_pool_dependency(repo_root: Path) -> None:
    pyproject = _load_toml(repo_root / "pyproject.toml")

    dependencies = pyproject["project"]["dependencies"]

    assert "psycopg[binary,pool]>=3.2" in dependencies


def test_uv_lock_records_psycopg_packages_and_gobby_requirement(repo_root: Path) -> None:
    lockfile = _load_toml(repo_root / "uv.lock")
    packages = {package["name"]: package for package in lockfile["package"]}

    for package_name in ("psycopg", "psycopg-binary", "psycopg-pool"):
        package = packages.get(package_name)
        assert package is not None, f"uv.lock is missing {package_name}"
        assert _version_tuple(package["version"]) >= (3, 2, 0)
        artifacts = [package["sdist"], *package.get("wheels", [])]
        assert artifacts, f"{package_name} has no locked artifacts"
        assert all("hash" in artifact for artifact in artifacts)

    gobby_package = packages["gobby"]
    requirements = gobby_package["metadata"]["requires-dist"]
    psycopg_requirement = next(
        (requirement for requirement in requirements if requirement["name"] == "psycopg"),
        None,
    )

    assert psycopg_requirement is not None
    assert psycopg_requirement["specifier"] == ">=3.2"
    assert set(psycopg_requirement["extras"]) == {"binary", "pool"}


def test_psycopg_and_pool_smoke_import_cleanly() -> None:
    psycopg = importlib.import_module("psycopg")
    psycopg_pool = importlib.import_module("psycopg_pool")

    assert _version_tuple(psycopg.__version__) >= (3, 2, 0)
    assert hasattr(psycopg_pool, "ConnectionPool")
