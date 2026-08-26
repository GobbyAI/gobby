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
_PHASE6_PGAUDIT_ASSET_FILES = ("initdb.d/02-pgaudit.sql",)
_BASELINE_EXTENSION_ASSET_FILES = ("initdb.d/03-pgcrypto.sql",)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _source_asset_root(repo_root: Path) -> Path:
    return repo_root / "src/gobby/data/postgres-pgsearch"


def _assert_asset_files_exist(root: Path, relative_paths: tuple[str, ...]) -> None:
    for relative_path in relative_paths:
        assert (root / relative_path).is_file(), f"missing {relative_path}"


def _read_source_asset(repo_root: Path, relative_path: str) -> str:
    path = _source_asset_root(repo_root) / relative_path
    assert path.is_file(), f"missing {relative_path}"
    return path.read_text()


def test_source_tree_has_phase1_assets(repo_root: Path) -> None:
    _assert_asset_files_exist(_source_asset_root(repo_root), _PHASE1_ASSET_FILES)


def test_source_tree_has_phase6_pgaudit_assets(repo_root: Path) -> None:
    _assert_asset_files_exist(_source_asset_root(repo_root), _PHASE6_PGAUDIT_ASSET_FILES)


def test_source_tree_has_baseline_extension_assets(repo_root: Path) -> None:
    _assert_asset_files_exist(_source_asset_root(repo_root), _BASELINE_EXTENSION_ASSET_FILES)


def test_version_manifest_schema_is_canonical(repo_root: Path) -> None:
    manifest = json.loads(_read_source_asset(repo_root, "version.json"))

    assert set(manifest) == {
        "pg_search_version",
        "pg_search_sha256",
        "pg_search_sha256_by_arch",
        "postgres_major",
    }
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["pg_search_version"])
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["pg_search_sha256"])
    assert set(manifest["pg_search_sha256_by_arch"]) == {"amd64", "arm64"}
    for sha256 in manifest["pg_search_sha256_by_arch"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", sha256)
    assert manifest["postgres_major"] == "18"
    assert manifest["pg_search_sha256_by_arch"]["amd64"] == (
        "6b042d61d156ca5fdcb1c417e291d90bffe3026848890be30bf6e578146b4676"
    )
    assert manifest["pg_search_sha256_by_arch"]["arm64"] == (
        "5ad13a80b76c46590914e0c366bd8deaf807d5b352f5ad489876ec836d06d3d1"
    )


def test_dockerfile_uses_manifest_build_args_and_initdb_seed(repo_root: Path) -> None:
    dockerfile = _read_source_asset(repo_root, "Dockerfile")

    assert (
        "FROM postgres:18-trixie@sha256:41da01536bc3ae26308cefb0c57235e7488001360bdb15191eb0b7955b570299"
        in dockerfile
    )
    assert "ARG PG_SEARCH_VERSION" in dockerfile
    assert "ARG PG_SEARCH_SHA256" in dockerfile
    assert 'test -n "$PG_SEARCH_VERSION"' in dockerfile
    assert 'test -n "$PG_SEARCH_SHA256"' in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "'postgresql-18-pgaudit=18.*'" in dockerfile
    assert "'curl=8.14.1-*'" in dockerfile
    assert "'ca-certificates=20250419*'" in dockerfile
    assert "postgresql-18-pg-search_${PG_SEARCH_VERSION}-1PARADEDB-trixie_${arch}.deb" in dockerfile
    assert "COPY initdb.d/ /docker-entrypoint-initdb.d/" in dockerfile


def test_dockerfile_preloads_extensions_without_audit_logging(repo_root: Path) -> None:
    dockerfile = _read_source_asset(repo_root, "Dockerfile")

    assert "shared_preload_libraries = 'pg_search,pgaudit'" in dockerfile
    assert "pgaudit.log" not in dockerfile
    assert "/var/log/pgaudit" not in dockerfile
    assert "pg_audit_export" not in dockerfile


def test_initdb_seed_installs_pg_search_extension(repo_root: Path) -> None:
    seed_sql = _read_source_asset(repo_root, "initdb.d/01-pg_search.sql")

    assert "CREATE EXTENSION IF NOT EXISTS pg_search;" in seed_sql


def test_pgaudit_seed_installs_only_the_extension(repo_root: Path) -> None:
    seed_sql = _read_source_asset(repo_root, "initdb.d/02-pgaudit.sql")

    assert seed_sql.strip() == "CREATE EXTENSION IF NOT EXISTS pgaudit;"


def test_pgcrypto_seed_installs_extension(repo_root: Path) -> None:
    seed_sql = _read_source_asset(repo_root, "initdb.d/03-pgcrypto.sql")

    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto;" in seed_sql


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


def test_installed_wheel_ships_phase6_pgaudit_asset_tree() -> None:
    asset_root = resources.files("gobby").joinpath("data/postgres-pgsearch")

    for relative_path in _PHASE6_PGAUDIT_ASSET_FILES:
        assert asset_root.joinpath(relative_path).is_file(), f"missing {relative_path}"


def test_installed_wheel_ships_baseline_extension_asset_tree() -> None:
    asset_root = resources.files("gobby").joinpath("data/postgres-pgsearch")

    for relative_path in _BASELINE_EXTENSION_ASSET_FILES:
        assert asset_root.joinpath(relative_path).is_file(), f"missing {relative_path}"


def test_sync_copies_complete_tree_at_install_time(tmp_path: Path) -> None:
    from gobby.cli.installers.postgres import _sync_postgres_pgsearch_assets

    _sync_postgres_pgsearch_assets(gobby_home=tmp_path)

    resource_root = resources.files("gobby").joinpath("data/postgres-pgsearch")
    target_root = tmp_path / "services/postgres-pgsearch"

    for relative_path in (
        _PHASE1_ASSET_FILES + _PHASE6_PGAUDIT_ASSET_FILES + _BASELINE_EXTENSION_ASSET_FILES
    ):
        assert (
            target_root.joinpath(relative_path).read_bytes()
            == resource_root.joinpath(relative_path).read_bytes()
        )

    assert not (tmp_path / "services/.env").exists()
