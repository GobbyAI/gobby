"""Tests for the PostgreSQL installer mode contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path
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
    assert installer.install_postgres(mode="docker", gobby_home=tmp_path)["mode"] == "docker"

    for stale_mode in ("native", "external"):
        with pytest.raises(click.ClickException, match="Docker is the only supported mode"):
            installer.install_postgres(mode=stale_mode)

    assert [mode for mode, _kwargs in calls] == ["docker"]


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


def test_pg_search_manifest_selects_current_arch_checksum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    asset_root = tmp_path / "postgres-pgsearch"
    asset_root.mkdir()
    (asset_root / "version.json").write_text(
        (
            '{"pg_search_version":"0.23.4",'
            '"pg_search_sha256":"amd64hash",'
            '"pg_search_sha256_by_arch":{"amd64":"amd64hash","arm64":"arm64hash"},'
            '"postgres_major":"18"}'
        ),
        encoding="utf-8",
    )

    class _FakeFiles:
        def joinpath(self, relative_path: str) -> Path:
            assert relative_path == "data/postgres-pgsearch/version.json"
            return asset_root / "version.json"

    monkeypatch.setattr(installer.resources, "files", lambda _package: _FakeFiles())
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")

    manifest = installer._read_pgsearch_version_manifest()

    assert manifest["pg_search_sha256"] == "arm64hash"


def test_write_bootstrap_defaults_surfaces_bootstrap_errors_as_click_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()

    def _fail_write(**_kwargs: Any) -> None:
        raise installer.BootstrapConfigError("bootstrap is invalid")

    monkeypatch.setattr(installer._bootstrap, "write_postgres_defaults", _fail_write)

    with pytest.raises(click.ClickException, match="bootstrap is invalid"):
        installer._write_bootstrap_defaults(
            gobby_home=tmp_path,
            mode="docker",
            database_url="postgresql://gobby:secret@localhost:60891/gobby",
        )


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
            return [("PostgreSQL 18.4 on x86_64-pc-linux-gnu",)]
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
    monkeypatch.setattr(
        installer,
        "_read_bootstrap_database_url",
        lambda _home: "postgresql://gobby:secret@example.com/gobby",
        raising=False,
    )

    status = await installer.get_postgres_status(
        gobby_home=tmp_path,
        mode="docker",
        dsn="postgresql://gobby:secret@example.com/gobby",
    )

    assert status["mode"] == "docker"
    assert status["dsn_host"] == "example.com"
    assert status["dsn_db"] == "gobby"
    assert isinstance(status["healthy"], bool)
    assert set(status["extensions"]) == {"pg_search", "pgaudit"}
    assert isinstance(status["preload_libraries"], list)
    assert "keyring" not in status


def test_render_postgres_status_omits_keyring_preflight() -> None:
    installer = _import_installer()

    rendered = installer.render_postgres_status(
        {
            "mode": "docker",
            "dsn_host": "localhost",
            "dsn_db": "gobby",
            "healthy": True,
            "extensions": {"pg_search": True, "pgaudit": False},
        }
    )

    assert "Keyring:" not in rendered
    assert "Migration:" not in rendered
