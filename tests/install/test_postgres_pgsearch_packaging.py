"""Packaging tests for the local-build postgres-pgsearch asset tree."""

from __future__ import annotations

import importlib.resources as resources
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PHASE1_ASSET_FILES = (
    "Dockerfile",
    "version.json",
    "initdb.d/01-pg_search.sql",
)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _source_asset_root(repo_root: Path) -> Path:
    return repo_root / "src/gobby/data/postgres-pgsearch"


def _assert_phase1_tree_exists(root: Path) -> None:
    for relative_path in _PHASE1_ASSET_FILES:
        assert (root / relative_path).is_file(), f"missing {relative_path}"


def _read_source_asset(repo_root: Path, relative_path: str) -> str:
    path = _source_asset_root(repo_root) / relative_path
    assert path.is_file(), f"missing {relative_path}"
    return path.read_text()


def test_source_tree_has_phase1_assets(repo_root: Path) -> None:
    _assert_phase1_tree_exists(_source_asset_root(repo_root))


def test_version_manifest_schema_is_canonical(repo_root: Path) -> None:
    manifest = json.loads(_read_source_asset(repo_root, "version.json"))

    assert set(manifest) == {"pg_search_version", "pg_search_sha256", "postgres_major"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["pg_search_version"])
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["pg_search_sha256"])
    assert manifest["postgres_major"] == "17"


def test_dockerfile_uses_manifest_build_args_and_initdb_seed(repo_root: Path) -> None:
    dockerfile = _read_source_asset(repo_root, "Dockerfile")

    assert "FROM postgres:17" in dockerfile
    assert "ARG PG_SEARCH_VERSION" in dockerfile
    assert "ARG PG_SEARCH_SHA256" in dockerfile
    assert 'test -n "$PG_SEARCH_VERSION"' in dockerfile
    assert 'test -n "$PG_SEARCH_SHA256"' in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "postgresql-17-pgaudit" in dockerfile
    assert "COPY initdb.d/ /docker-entrypoint-initdb.d/" in dockerfile


def test_initdb_seed_installs_pg_search_extension(repo_root: Path) -> None:
    seed_sql = _read_source_asset(repo_root, "initdb.d/01-pg_search.sql")

    assert "CREATE EXTENSION IF NOT EXISTS pg_search;" in seed_sql


def test_package_data_recursively_includes_gobby_data(repo_root: Path) -> None:
    pyproject = _load_toml(repo_root / "pyproject.toml")

    package_data = pyproject["tool"]["setuptools"]["package-data"]["gobby"]

    assert "data/**/*" in package_data


def test_runtime_resource_lookup_uses_gobby_package_joinpath(repo_root: Path) -> None:
    source_text = "\n".join(path.read_text() for path in (repo_root / "src/gobby").rglob("*.py"))

    assert 'resources.files("gobby").joinpath("data/postgres-pgsearch")' in source_text
    assert "gobby.data.postgres-pgsearch" not in source_text


def test_installed_wheel_ships_phase1_asset_tree() -> None:
    asset_root = resources.files("gobby").joinpath("data/postgres-pgsearch")

    for relative_path in _PHASE1_ASSET_FILES:
        assert asset_root.joinpath(relative_path).is_file(), f"missing {relative_path}"


def test_sync_copies_complete_tree_at_install_time(tmp_path: Path) -> None:
    from gobby.cli.installers.postgres import (
        _sync_postgres_pgsearch_assets,
        _write_compose_env,
    )

    _sync_postgres_pgsearch_assets(gobby_home=tmp_path)
    _write_compose_env(gobby_home=tmp_path)

    resource_root = resources.files("gobby").joinpath("data/postgres-pgsearch")
    target_root = tmp_path / "services/postgres-pgsearch"

    for relative_path in _PHASE1_ASSET_FILES:
        assert (
            target_root.joinpath(relative_path).read_bytes()
            == resource_root.joinpath(relative_path).read_bytes()
        )

    manifest = json.loads(resource_root.joinpath("version.json").read_text())
    env_text = (tmp_path / "services/.env").read_text()
    assert f"GOBBY_PG_SEARCH_VERSION={manifest['pg_search_version']}" in env_text
    assert f"GOBBY_PG_SEARCH_SHA256={manifest['pg_search_sha256']}" in env_text
