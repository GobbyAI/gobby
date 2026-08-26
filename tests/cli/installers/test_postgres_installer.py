"""Tests for the PostgreSQL installer mode contracts."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import click
import pytest
import yaml

pytestmark = pytest.mark.unit


def _import_installer() -> Any:
    from gobby.cli.installers import postgres

    return postgres


def _completed_process(args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args or [], returncode=0, stdout="", stderr="")


_PERSISTED_DSN = "postgresql://gobby:persisted-password@localhost:60991/gobby"


def _write_bootstrap(gobby_home: Path, files_home: Path, database_url: str) -> Path:
    bootstrap = gobby_home / "bootstrap.yaml"
    bootstrap.write_text(
        f"hub_backend: postgres\nfiles_home: {files_home}\ndatabase_url: {database_url}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    return bootstrap


def _refuse_password_minting(monkeypatch: pytest.MonkeyPatch, installer: Any) -> None:
    monkeypatch.setattr(
        installer.secrets,
        "token_urlsafe",
        lambda _size: pytest.fail("the installer must never mint a PostgreSQL password"),
    )


def test_install_postgres_uses_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    assert installer.install_postgres(gobby_home=tmp_path)["mode"] == "docker"

    assert [mode for mode, _kwargs in calls] == ["docker"]


def test_docker_install_runs_postgres_profile_and_writes_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    subprocess_calls: list[list[str]] = []
    subprocess_envs: list[dict[str, str]] = []
    helper_calls: list[str] = []
    bootstrap_payloads: list[dict[str, Any]] = []

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(args)
        subprocess_envs.append(kwargs.get("env", {}))
        return _completed_process(args)

    def _write_bootstrap_defaults(*args: Any, **kwargs: Any) -> None:
        bootstrap_payloads.append({"args": args, "kwargs": kwargs})

    def _record_asset_sync(**_kwargs: Any) -> None:
        helper_calls.append("sync_assets")

    def _record_probe(**kwargs: Any) -> None:
        helper_calls.append(f"probe:{kwargs['sql']}")

    def _record_readiness(**_kwargs: Any) -> bool:
        helper_calls.append("pg_isready")
        return True

    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(installer.subprocess, "run", _run)
    monkeypatch.setenv("GOBBY_POSTGRES_PASSWORD", "transient-password")
    _refuse_password_minting(monkeypatch, installer)
    monkeypatch.setattr(
        installer,
        "_sync_postgres_pgsearch_assets",
        _record_asset_sync,
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_wait_for_pg_isready",
        _record_readiness,
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_probe_create_extension",
        _record_probe,
        raising=False,
    )
    monkeypatch.setattr(
        installer,
        "_write_bootstrap_defaults",
        _write_bootstrap_defaults,
        raising=False,
    )
    files_home = tmp_path / "files"
    files_home.mkdir()
    _write_bootstrap(tmp_path, files_home, _PERSISTED_DSN)

    result = installer._install_docker(gobby_home=tmp_path, files_home=files_home)

    assert result["success"] is True
    assert helper_calls == [
        "sync_assets",
        "pg_isready",
        "probe:CREATE EXTENSION IF NOT EXISTS pg_search",
        "probe:CREATE EXTENSION IF NOT EXISTS pgaudit",
        "probe:CREATE EXTENSION IF NOT EXISTS pgcrypto",
    ]
    assert subprocess_calls
    compose_up = subprocess_calls[0]
    assert compose_up[:2] == ["docker", "compose"]
    assert "--profile" in compose_up
    assert "postgres" in compose_up
    assert "up" in compose_up
    assert "-d" in compose_up
    assert subprocess_envs[0]["GOBBY_POSTGRES_PASSWORD"] == "persisted-password"
    assert not (tmp_path / "services" / ".env").exists()
    assert bootstrap_payloads == [
        {"args": (), "kwargs": {"gobby_home": tmp_path, "database_url": _PERSISTED_DSN}}
    ]


def test_persisted_database_url_wins_over_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    files_home = tmp_path / "files"
    files_home.mkdir()
    _write_bootstrap(tmp_path, files_home, _PERSISTED_DSN)
    monkeypatch.setenv("GOBBY_POSTGRES_PASSWORD", "transient-password")
    _refuse_password_minting(monkeypatch, installer)

    database_url, runtime = installer._resolve_postgres_install_database_url(gobby_home=tmp_path)

    assert database_url == _PERSISTED_DSN
    assert runtime.environment["GOBBY_POSTGRES_PASSWORD"] == "persisted-password"


def test_missing_database_url_fails_before_compose_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    files_home = tmp_path / "files"
    files_home.mkdir()
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(installer, "_sync_postgres_pgsearch_assets", lambda **_kwargs: None)
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("compose must not run without a database_url"),
    )
    monkeypatch.setenv("GOBBY_POSTGRES_PASSWORD", "transient-password")
    _refuse_password_minting(monkeypatch, installer)

    result = installer._install_docker(gobby_home=tmp_path, files_home=files_home)

    assert result["success"] is False
    assert (
        result["error"] == f"{tmp_path / 'bootstrap.yaml'} has no database_url; run `gobby install`"
    )


def test_readiness_failure_names_bootstrap_and_volume_without_removing_anything(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    subprocess_calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(args)
        return _completed_process(args)

    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(installer.subprocess, "run", _run)
    monkeypatch.setattr(installer, "_sync_postgres_pgsearch_assets", lambda **_kwargs: None)
    monkeypatch.setattr(installer, "_wait_for_pg_isready", lambda **_kwargs: False)
    monkeypatch.setattr(
        installer,
        "_write_bootstrap_defaults",
        lambda **_kwargs: pytest.fail("a failed install must leave bootstrap.yaml untouched"),
    )
    files_home = tmp_path / "files"
    files_home.mkdir()
    _write_bootstrap(tmp_path, files_home, _PERSISTED_DSN)

    result = installer._install_docker(gobby_home=tmp_path, files_home=files_home)

    assert result["success"] is False
    assert "PostgreSQL did not become ready" in result["error"]
    assert "gobby_postgres_data" in result["error"]
    assert f"database_url in {tmp_path / 'bootstrap.yaml'}" in result["error"]
    assert "Gobby never removes data volumes" in result["error"]
    assert [call[:2] for call in subprocess_calls] == [["docker", "compose"]]
    assert "up" in subprocess_calls[0]
    assert not any(verb in call for call in subprocess_calls for verb in ("down", "rm", "volume"))


def test_docker_install_refuses_compose_without_files_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    calls: list[str] = []

    def _compose(*_args: object, **_kwargs: object) -> None:
        calls.append("compose")
        raise AssertionError("compose must not run without files_home")

    def _assets(**_kwargs: object) -> None:
        calls.append("assets")

    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(installer, "reconcile_unified_compose", _compose)
    monkeypatch.setattr(installer, "_sync_postgres_pgsearch_assets", _assets)

    result = installer._install_docker(gobby_home=tmp_path)

    assert result["success"] is False
    assert "files-home" in result["error"]
    assert calls == []


def test_postgres_install_refreshes_stale_unified_compose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    compose_file = services_dir / "docker-compose.yml"
    compose_file.write_text(
        'services:\n  qdrant:\n    image: qdrant/qdrant:old\n    restart: "no"\n',
        encoding="utf-8",
    )
    bundled_compose = tmp_path / "bundled-compose.yml"
    bundled_compose.write_text(
        "services:\n  qdrant:\n    image: qdrant/qdrant:latest\n    restart: unless-stopped\n"
        "  postgres:\n    image: postgres:18\n    restart: unless-stopped\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_COMPOSE_SRC", bundled_compose)

    result = installer.reconcile_unified_compose(services_dir)

    assert result.compose_file == compose_file
    assert result.refreshed is True
    assert result.changed_services == frozenset({"postgres", "qdrant"})
    deployed = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    assert deployed["services"]["qdrant"]["image"] == "qdrant/qdrant:latest"
    assert deployed["services"]["qdrant"]["restart"] == "no"
    assert deployed["services"]["postgres"]["restart"] == "no"


def test_matching_unified_compose_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    compose_file = services_dir / "docker-compose.yml"
    bundled_compose = tmp_path / "bundled-compose.yml"
    content = "services:\n  postgres:\n    image: postgres:18\n    restart: unless-stopped\n"
    compose_file.write_text(content, encoding="utf-8")
    bundled_compose.write_text(content, encoding="utf-8")
    monkeypatch.setattr(installer, "_COMPOSE_SRC", bundled_compose)
    fixed_timestamp = 1_700_000_000
    os.utime(compose_file, (fixed_timestamp, fixed_timestamp))
    original_mtime = compose_file.stat().st_mtime_ns

    result = installer.reconcile_unified_compose(services_dir)

    assert result.refreshed is False
    assert result.changed_services == frozenset()
    assert compose_file.stat().st_mtime_ns == original_mtime


def test_reconciliation_reports_changed_services_not_started_by_installer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    (services_dir / "docker-compose.yml").write_text(
        "services:\n  postgres:\n    image: postgres:17\n  qdrant:\n    image: qdrant/qdrant:old\n",
        encoding="utf-8",
    )
    bundled_compose = tmp_path / "bundled-compose.yml"
    bundled_compose.write_text(
        "services:\n  postgres:\n    image: postgres:18\n"
        "  qdrant:\n    image: qdrant/qdrant:latest\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_COMPOSE_SRC", bundled_compose)

    reconciliation = installer.reconcile_unified_compose(services_dir)
    notice = installer.compose_restart_required_notice(
        reconciliation,
        started_services=frozenset({"postgres"}),
    )

    assert (
        notice == "Managed Compose definitions changed for qdrant; restart required to apply them."
    )


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
            database_url="postgresql://gobby:secret@localhost:60891/gobby",
        )


class _FakeCursor:
    def __init__(
        self,
        statements: list[str],
        *,
        pg_search_present: bool = True,
        pgcrypto_present: bool = True,
    ) -> None:
        self.statements = statements
        self.pg_search_present = pg_search_present
        self.pgcrypto_present = pgcrypto_present
        self.current_sql = ""
        self.current_params: object | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, _params: object | None = None) -> _FakeCursor:
        self.current_sql = sql
        self.current_params = _params
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
        if "pg_extension" in sql and self.current_params == ("pg_search",):
            return [(1,)] if self.pg_search_present else []
        if "pg_extension" in sql and self.current_params == ("pgcrypto",):
            return [(1,)] if self.pgcrypto_present else []
        if "pg_available_extensions" in sql:
            return [("pgaudit",)]
        if "version()" in sql:
            return [("PostgreSQL 18.4 on x86_64-pc-linux-gnu",)]
        return []


class _FakeConnection:
    def __init__(
        self,
        statements: list[str],
        *,
        pg_search_present: bool = True,
        pgcrypto_present: bool = True,
    ) -> None:
        self.cursor_obj = _FakeCursor(
            statements,
            pg_search_present=pg_search_present,
            pgcrypto_present=pgcrypto_present,
        )
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
        dsn="postgresql://gobby:secret@example.com/gobby",
    )

    assert "mode" not in status
    assert status["dsn_host"] == "example.com"
    assert status["dsn_db"] == "gobby"
    assert isinstance(status["healthy"], bool)
    assert set(status["extensions"]) == {"pg_search", "pgaudit", "pgcrypto"}
    assert isinstance(status["preload_libraries"], list)


@pytest.mark.asyncio
async def test_get_postgres_status_honors_gobby_home_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = _import_installer()
    configured_home = tmp_path / "custom-gobby-home"
    configured_home.mkdir()
    bootstrap_path = configured_home / "bootstrap.yaml"
    files_home = configured_home / "files"
    files_home.mkdir()
    bootstrap_path.write_text(
        "hub_backend: postgres\n"
        f"files_home: {files_home}\n"
        "database_url: postgresql://invalid:invalid@127.0.0.1:1/custom_home_db\n"
    )
    bootstrap_path.chmod(0o600)
    monkeypatch.setenv("GOBBY_HOME", str(configured_home))

    status = await installer.get_postgres_status(
        readiness_timeout=1,
        connect_timeout=1,
    )

    assert status["dsn_host"] == "127.0.0.1"
    assert status["dsn_db"] == "custom_home_db"
    assert status["healthy"] is False


def test_render_postgres_status_omits_legacy_preflight_sections() -> None:
    installer = _import_installer()

    rendered = installer.render_postgres_status(
        {
            "mode": "docker",
            "dsn_host": "localhost",
            "dsn_db": "gobby",
            "healthy": True,
            "extensions": {"pg_search": True, "pgaudit": False, "pgcrypto": True},
        }
    )

    assert "Migration:" not in rendered
    assert "pgcrypto:    yes" in rendered


@pytest.mark.asyncio
async def test_get_postgres_status_runs_its_blocking_work_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every step of the status payload blocks, so none of it may run on the loop.

    The /health dashboard route awaits this function. Its body forks
    ``pg_isready``, parses bootstrap.yaml with ``realpath`` checks, opens a
    fresh psycopg connection, and runs five round trips -- all synchronous. On
    the loop it stalled the daemon for seconds at a time (#20845).
    """
    installer = _import_installer()
    statements: list[str] = []
    worker_threads: list[int] = []

    def record_connect(*_args: Any, **_kwargs: Any) -> Any:
        worker_threads.append(threading.get_ident())
        return _FakeConnection(statements)

    def record_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        worker_threads.append(threading.get_ident())
        return _completed_process()

    def record_bootstrap_read(_home: Path) -> str:
        worker_threads.append(threading.get_ident())
        return "postgresql://gobby:secret@example.com/gobby"

    monkeypatch.setattr(installer.psycopg, "connect", record_connect)
    monkeypatch.setattr(installer.subprocess, "run", record_run)
    monkeypatch.setattr(
        installer,
        "_read_bootstrap_database_url",
        record_bootstrap_read,
        raising=False,
    )

    loop_thread = threading.get_ident()
    await installer.get_postgres_status(gobby_home=tmp_path)

    assert worker_threads, "no blocking step ran"
    assert loop_thread not in worker_threads
