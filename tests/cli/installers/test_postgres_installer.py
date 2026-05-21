"""Tests for the PostgreSQL installer mode contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest

pytestmark = pytest.mark.unit


def _import_installer() -> Any:
    from gobby.cli.installers import postgres

    return postgres


def _completed_process(args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args or [], returncode=0, stdout="", stderr="")


def test_install_postgres_dispatches_modes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    installer = _import_installer()
    calls: list[tuple[str, dict[str, Any]]] = []

    def _record(mode: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((mode, kwargs))
        return {"success": True, "mode": mode}

    monkeypatch.setattr(
        installer,
        "_install_docker",
        lambda **kwargs: _record("docker", **kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_install_native",
        lambda **kwargs: _record("native", **kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_install_external",
        lambda **kwargs: _record("external", **kwargs),
        raising=False,
    )

    assert installer.install_postgres(mode="docker", gobby_home=tmp_path)["mode"] == "docker"
    assert installer.install_postgres(mode="native", dsn="postgresql://local/db")["mode"] == (
        "native"
    )
    assert installer.install_postgres(mode="external", dsn="postgresql://remote/db")["mode"] == (
        "external"
    )
    with pytest.raises(click.ClickException, match="--mode external requires --dsn"):
        installer.install_postgres(mode="external")

    assert [mode for mode, _kwargs in calls] == ["docker", "native", "external"]


def test_docker_install_runs_postgres_profile_and_writes_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    subprocess_calls: list[list[str]] = []
    helper_calls: list[str] = []
    bootstrap_payloads: list[dict[str, Any]] = []

    def _run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(args)
        return _completed_process(args)

    def _write_bootstrap_defaults(*args: Any, **kwargs: Any) -> None:
        bootstrap_payloads.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(installer.subprocess, "run", _run)
    monkeypatch.setattr(
        installer,
        "_sync_postgres_pgsearch_assets",
        lambda **_kwargs: helper_calls.append("sync_assets"),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_write_compose_env",
        lambda **_kwargs: helper_calls.append("write_env"),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_wait_for_pg_isready",
        lambda **_kwargs: helper_calls.append("pg_isready") or True,
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_probe_create_pg_search_extension",
        lambda **_kwargs: helper_calls.append("probe_pg_search"),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_write_bootstrap_defaults",
        _write_bootstrap_defaults,
        raising=False,
    )

    result = installer._install_docker(gobby_home=tmp_path, port=60991)

    assert result["success"] is True
    assert helper_calls == ["sync_assets", "write_env", "pg_isready", "probe_pg_search"]
    assert subprocess_calls
    compose_up = subprocess_calls[0]
    assert compose_up[:2] == ["docker", "compose"]
    assert "--profile" in compose_up
    assert "postgres" in compose_up
    assert "up" in compose_up
    assert "-d" in compose_up
    payload_text = repr(bootstrap_payloads)
    assert "postgresql://" in payload_text
    assert "localhost:60991" in payload_text
    assert "/gobby" in payload_text
    assert "docker" in payload_text


def test_postgres_install_refreshes_stale_unified_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    compose_file = services_dir / "docker-compose.yml"
    compose_file.write_text("services:\n  qdrant: {}\n", encoding="utf-8")
    bundled_compose = tmp_path / "bundled-compose.yml"
    bundled_compose.write_text("services:\n  postgres: {}\n", encoding="utf-8")
    monkeypatch.setattr(installer, "_COMPOSE_SRC", bundled_compose)

    result = installer._ensure_unified_compose(services_dir)

    assert result == compose_file
    assert "postgres" in compose_file.read_text(encoding="utf-8")
    assert "qdrant" not in compose_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("platform_info", "runbook"),
    [
        (SimpleNamespace(os="darwin", distro="macos", arch="arm64"), "postgres-native-macos.md"),
        (SimpleNamespace(os="linux", distro="fedora", arch="x86_64"), "postgres-native-source.md"),
    ],
)
def test_native_install_refuses_unsupported_platforms_with_runbook_and_docker_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform_info: SimpleNamespace,
    runbook: str,
) -> None:
    installer = _import_installer()

    monkeypatch.setattr(installer, "_detect_platform", lambda: platform_info, raising=False)

    with pytest.raises(click.ClickException) as exc_info:
        installer._install_native(gobby_home=tmp_path, dsn=None)

    message = str(exc_info.value)
    assert runbook in message
    assert "--mode docker" in message


def test_native_debian_installs_sha_pinned_deb_and_probes_extension(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    manifest = {
        "pg_search_version": "0.17.0",
        "pg_search_sha256": "a" * 64,
        "postgres_major": "17",
    }
    deb_path = tmp_path / "pg_search.deb"
    records: list[tuple[str, dict[str, Any]]] = []

    def _record(name: str, **kwargs: Any) -> None:
        records.append((name, kwargs))

    monkeypatch.setattr(
        installer,
        "_detect_platform",
        lambda: SimpleNamespace(os="linux", distro="ubuntu", arch="x86_64"),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_read_pgsearch_version_manifest",
        lambda: manifest,
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_download_pg_search_deb",
        lambda **kwargs: _record("download", **kwargs) or deb_path,
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_install_deb_with_sudo",
        lambda **kwargs: _record("dpkg", **kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_probe_create_pg_search_extension",
        lambda **kwargs: _record("probe", **kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_write_bootstrap_defaults",
        lambda **kwargs: _record("bootstrap", **kwargs),
        raising=False,
    )

    result = installer._install_native(
        gobby_home=tmp_path,
        dsn="postgresql://gobby:secret@localhost:5432/gobby",
    )

    assert result["success"] is True
    assert [name for name, _kwargs in records] == ["download", "dpkg", "probe", "bootstrap"]
    assert records[0][1]["version"] == "0.17.0"
    assert records[0][1]["sha256"] == "a" * 64
    assert records[2][1]["sql"] == "CREATE EXTENSION pg_search"


class _FakeCursor:
    def __init__(self, statements: list[str], *, pg_search_present: bool = True) -> None:
        self.statements = statements
        self.pg_search_present = pg_search_present
        self.current_sql = ""

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, _params: object | None = None) -> _FakeCursor:
        self.current_sql = sql
        self.statements.append(sql)
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows()

    def fetchone(self) -> tuple[object, ...] | None:
        rows = self._rows()
        return rows[0] if rows else None

    def _rows(self) -> list[tuple[object, ...]]:
        sql = self.current_sql.lower()
        if "pg_namespace" in sql:
            return [("public",)]
        if "pg_class" in sql or "pg_proc" in sql or "pg_type" in sql:
            return []
        if "pg_extension" in sql and "pg_search" in sql:
            return [(1,)] if self.pg_search_present else []
        if "pg_available_extensions" in sql:
            return [("pgaudit",)]
        if "version()" in sql:
            return [("PostgreSQL 17.2 on x86_64-pc-linux-gnu",)]
        return []


class _FakeConnection:
    def __init__(self, statements: list[str], *, pg_search_present: bool = True) -> None:
        self.cursor_obj = _FakeCursor(statements, pg_search_present=pg_search_present)
        self.committed = False

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def execute(self, sql: str, params: object | None = None) -> _FakeCursor:
        return self.cursor_obj.execute(sql, params)

    def commit(self) -> None:
        self.committed = True


def test_external_install_read_only_probes_before_writing_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    statements: list[str] = []

    monkeypatch.setattr(
        installer.psycopg,
        "connect",
        lambda *_args, **_kwargs: _FakeConnection(statements),
    )
    monkeypatch.setattr(
        installer,
        "_write_bootstrap_defaults",
        lambda **_kwargs: None,
        raising=False,
    )

    result = installer._install_external(
        gobby_home=tmp_path,
        dsn="postgresql://gobby:secret@example.com/gobby",
    )

    assert result["success"] is True
    upper_statements = [statement.upper() for statement in statements]
    sentinel_index = next(
        index
        for index, statement in enumerate(upper_statements)
        if "GOBBY_INSTALL_OWNERSHIP" in statement
    )
    assert any("PG_NAMESPACE" in statement for statement in upper_statements[:sentinel_index])
    assert any("PG_CLASS" in statement for statement in upper_statements[:sentinel_index])
    assert any("PG_PROC" in statement for statement in upper_statements[:sentinel_index])
    assert any("PG_TYPE" in statement for statement in upper_statements[:sentinel_index])
    assert any("PG_EXTENSION" in statement for statement in upper_statements[:sentinel_index])
    assert any("PG_AVAILABLE_EXTENSIONS" in statement for statement in upper_statements)
    assert all("CREATE EXTENSION" not in statement for statement in upper_statements)
    assert any(
        "CREATE TABLE GOBBY_INSTALL_OWNERSHIP" in statement for statement in upper_statements
    )
    assert any("INSERT INTO GOBBY_INSTALL_OWNERSHIP" in statement for statement in upper_statements)


def test_external_install_refuses_missing_pg_search_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    statements: list[str] = []

    monkeypatch.setattr(
        installer.psycopg,
        "connect",
        lambda *_args, **_kwargs: _FakeConnection(statements, pg_search_present=False),
    )

    with pytest.raises(click.ClickException, match="pg_search"):
        installer._install_external(
            gobby_home=tmp_path,
            dsn="postgresql://gobby:secret@example.com/gobby",
        )

    upper_statements = [statement.upper() for statement in statements]
    assert all("CREATE EXTENSION" not in statement for statement in upper_statements)
    assert all("GOBBY_INSTALL_OWNERSHIP" not in statement for statement in upper_statements)


@pytest.mark.asyncio
async def test_get_postgres_status_returns_stable_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    statements: list[str] = []

    monkeypatch.setattr(
        installer.psycopg,
        "connect",
        lambda *_args, **_kwargs: _FakeConnection(statements),
    )
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs: _completed_process())

    status = await installer.get_postgres_status(
        gobby_home=tmp_path,
        mode="external",
        dsn="postgresql://gobby:secret@example.com/gobby",
    )

    assert status["mode"] == "external"
    assert status["dsn_host"] == "example.com"
    assert status["dsn_db"] == "gobby"
    assert isinstance(status["healthy"], bool)
    assert set(status["extensions"]) == {"pg_search", "pgaudit"}
    assert isinstance(status["preload_libraries"], list)
    assert set(status["migration_complete"]) == {"present", "imported_at"}
    assert set(status["ownership"]) == {"sentinel_present", "installed_at", "gobby_version"}
