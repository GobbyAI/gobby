"""CLI wiring tests for the PostgreSQL command group."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_postgres_group_is_registered_on_root_cli() -> None:
    from gobby.cli import cli

    assert "postgres" in cli.commands
    postgres_group = cli.commands["postgres"]
    assert {"install", "uninstall", "status", "activate", "deactivate"} <= set(
        postgres_group.commands
    )


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["postgres", "install", "--help"], ["--mode", "--dsn", "docker"]),
        (["postgres", "status", "--help"], ["--json"]),
        (["postgres", "uninstall", "--help"], ["--remove-data"]),
        (["postgres", "activate", "--help"], ["--capture-sink", "--accept-no-rollback-risk"]),
        (["postgres", "deactivate", "--help"], []),
    ],
)
def test_postgres_cli_help_exposes_phase1_options(args: list[str], expected: list[str]) -> None:
    from gobby.cli import cli

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0
    for text in expected:
        assert text in result.output


def test_postgres_cli_exposes_migrate_from_sqlite_command() -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli import cli

    assert callable(getattr(postgres_cli_module, "migrate_from_sqlite", None))
    assert "migrate-from-sqlite" in postgres_cli_module.postgres_cli.commands
    assert "migrate-from-sqlite" in cli.commands["postgres"].commands

    result = CliRunner().invoke(cli, ["postgres", "migrate-from-sqlite", "--help"])

    assert result.exit_code == 0
    assert "--source" in result.output
    assert "--target" in result.output
    assert "--batch-size" in result.output
    assert "--dry-run" in result.output


def test_migrate_from_sqlite_command_invokes_importer_with_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    source = tmp_path / "gobby-hub.db"
    source.write_bytes(b"sqlite fixture")
    calls: list[dict[str, Any]] = []

    def _migrate_sqlite_to_postgres(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"rows": 42, "tables": 7, "dry_run": kwargs["dry_run"]}

    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)
    monkeypatch.setattr(
        postgres_cli_module,
        "migrate_sqlite_to_postgres",
        _migrate_sqlite_to_postgres,
        raising=False,
    )

    result = CliRunner().invoke(
        postgres_cli,
        [
            "migrate-from-sqlite",
            "--source",
            str(source),
            "--target",
            "postgresql://gobby:secret@example.com/gobby",
            "--batch-size",
            "17",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "source": source,
            "target": "postgresql://gobby:secret@example.com/gobby",
            "batch_size": 17,
            "dry_run": True,
        }
    ]
    assert "dry-run: would import 42 rows across 7 tables" in result.output


def test_postgres_install_command_calls_installer_and_renders_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    calls: list[dict[str, Any]] = []

    def _install_postgres(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "success": True,
            "mode": kwargs["mode"],
            "database_url": kwargs["dsn"],
            "message": "PostgreSQL configured",
        }

    monkeypatch.setattr(postgres_cli_module, "install_postgres", _install_postgres)

    result = CliRunner().invoke(
        postgres_cli,
        [
            "install",
            "--mode",
            "external",
            "--dsn",
            "postgresql://gobby:secret@example.com/gobby",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "mode": "external",
            "dsn": "postgresql://gobby:secret@example.com/gobby",
        }
    ]
    assert "PostgreSQL configured" in result.output


def test_postgres_status_json_emits_structured_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    async def _status() -> dict[str, Any]:
        return {
            "mode": "docker",
            "dsn_host": "localhost",
            "dsn_db": "gobby",
            "healthy": True,
            "extensions": {"pg_search": True, "pgaudit": True},
            "preload_libraries": ["pg_search", "pgaudit"],
            "migration_complete": {"present": False, "imported_at": None},
        }

    monkeypatch.setattr(postgres_cli_module, "get_postgres_status", _status)

    result = CliRunner().invoke(postgres_cli, ["status", "--json"])

    assert result.exit_code == 0
    assert '"migration_complete"' in result.output
    assert '"pg_search": true' in result.output


def test_postgres_activate_refuses_when_daemon_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: True)

    result = CliRunner().invoke(postgres_cli, ["activate"])

    assert result.exit_code != 0
    assert "Stop the daemon first: gobby stop" in result.output


def test_postgres_activate_refuses_until_migration_completion_marker_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)
    monkeypatch.setattr(postgres_cli_module, "_postgres_migration_complete", lambda: False)

    result = CliRunner().invoke(postgres_cli, ["activate"])

    assert result.exit_code != 0
    assert "Run `gobby postgres migrate-from-sqlite` first" in result.output


def test_postgres_activate_docker_flips_bootstrap_and_writes_cutover_ticket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path, mode="docker", hub_backend="sqlite")
    _allow_activation(monkeypatch, postgres_cli_module, tmp_path=tmp_path, mode="docker")

    probe_calls: list[str] = []

    def _probe_pgaudit() -> dict[str, str]:
        probe_calls.append("probe")
        return {
            "audit_file": "/var/log/pgaudit/pgaudit.log",
            "audit_readback": "LOG:  AUDIT: SESSION,1,1,WRITE,UPDATE",
            "write_probe": "ok",
        }

    monkeypatch.setattr(
        postgres_cli_module,
        "_probe_pgaudit_or_fail",
        _probe_pgaudit,
    )

    result = CliRunner().invoke(postgres_cli, ["activate"])

    assert result.exit_code == 0
    assert probe_calls == ["probe"]
    assert _read_bootstrap(tmp_path)["hub_backend"] == "postgres"
    assert list(tmp_path.glob("bootstrap.yaml.*.bak"))
    tickets = list((tmp_path / "migrations").glob("cutover-*.json"))
    assert len(tickets) == 1
    payload = json.loads(tickets[0].read_text(encoding="utf-8"))
    assert payload["mode"] == "docker"
    assert payload["capture_kind"] == "pgaudit-managed"
    assert payload["capture_value"] is None
    assert payload["verification"]["state"] == "ok"
    assert payload["verification"]["probe_detail"]["audit_file"] == "/var/log/pgaudit/pgaudit.log"
    assert "AUDIT: SESSION" in payload["verification"]["probe_detail"]["audit_readback"]
    assert "_path" not in payload
    assert "PostgreSQL is the only supported runtime backend." in result.output
    assert "SQLite runtime deactivation is unsupported." in result.output
    assert "gobby postgres deactivate" not in result.output
    assert "Validation-window deadline:" in result.output


def test_postgres_activate_external_accepts_operator_capture_sink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path, mode="external", hub_backend="sqlite")
    _allow_activation(monkeypatch, postgres_cli_module, tmp_path=tmp_path, mode="external")
    ownership_checks: list[str] = []
    audit_log = tmp_path / "pgaudit.log"
    audit_log.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        postgres_cli_module,
        "_require_ownership_sentinel_or_fail",
        lambda: ownership_checks.append("checked"),
    )

    result = CliRunner().invoke(
        postgres_cli,
        ["activate", "--capture-sink", f"pgaudit-file:{audit_log}"],
    )

    assert result.exit_code == 0
    assert ownership_checks == ["checked"]
    assert _read_bootstrap(tmp_path)["hub_backend"] == "postgres"
    [ticket_path] = list((tmp_path / "migrations").glob("cutover-*.json"))
    payload = json.loads(ticket_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "external"
    assert payload["capture_kind"] == "pgaudit-file"
    assert payload["capture_value"] == str(audit_log)


def test_postgres_activate_external_rejects_missing_pgaudit_file_sink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path, mode="external", hub_backend="sqlite")
    _allow_activation(monkeypatch, postgres_cli_module, tmp_path=tmp_path, mode="external")
    monkeypatch.setattr(postgres_cli_module, "_require_ownership_sentinel_or_fail", lambda: None)

    result = CliRunner().invoke(
        postgres_cli,
        ["activate", "--capture-sink", f"pgaudit-file:{tmp_path / 'missing.log'}"],
    )

    assert result.exit_code != 0
    assert "pgaudit-file capture sink must already exist" in result.output
    assert _read_bootstrap(tmp_path)["hub_backend"] == "sqlite"


def test_probe_capture_sink_wal_archive_requires_matching_replication_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module

    queries: list[tuple[str, tuple[str, ...]]] = []

    class _FakeConnection:
        def execute(self, query: str, params: tuple[str, ...]) -> SimpleNamespace:
            queries.append((query, params))
            return SimpleNamespace(fetchone=lambda: None)

    @contextmanager
    def _postgres_context() -> Any:
        yield _FakeConnection()

    monkeypatch.setattr(postgres_cli_module, "_postgres_connection", _postgres_context)

    with pytest.raises(click.ClickException, match="replication slot 'gobby_slot' was not found"):
        postgres_cli_module._probe_capture_sink_or_fail("wal-archive", "gobby_slot")

    assert queries == [
        (
            "SELECT slot_name FROM pg_replication_slots WHERE slot_name = %s",
            ("gobby_slot",),
        )
    ]


def test_probe_capture_sink_wal_archive_extracts_slot_from_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module

    class _FakeConnection:
        def execute(self, _query: str, params: tuple[str, ...]) -> SimpleNamespace:
            assert params == ("gobby_slot",)
            return SimpleNamespace(fetchone=lambda: ("gobby_slot",))

    @contextmanager
    def _postgres_context() -> Any:
        yield _FakeConnection()

    monkeypatch.setattr(postgres_cli_module, "_postgres_connection", _postgres_context)

    result = postgres_cli_module._probe_capture_sink_or_fail(
        "wal-archive",
        "postgresql://archive.example/gobby?slot_name=gobby_slot",
    )

    assert result["capture_value"] == "postgresql://archive.example/gobby?slot_name=gobby_slot"
    assert result["replication_slot"] == "gobby_slot"


def test_probe_pgaudit_or_fail_reads_back_docker_audit_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module

    class _FakeConnection:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.committed = False

        def __enter__(self) -> _FakeConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str) -> SimpleNamespace:
            self.queries.append(query)
            if "pg_settings" in query:
                return SimpleNamespace(fetchone=lambda: ("write",))
            if "UPDATE _pgaudit_probe" in query:
                return SimpleNamespace(fetchone=lambda: ("2026-05-21T00:00:00Z",))
            raise AssertionError(f"unexpected query: {query}")

        def commit(self) -> None:
            self.committed = True

    fake_conn = _FakeConnection()
    commands: list[list[str]] = []

    def _run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        assert cmd[:4] == ["docker", "exec", "gobby-postgres", "sh"]
        assert "gobby-pgaudit-probe-fixed" in cmd[-1]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "/var/log/pgaudit/pgaudit.log\n"
                "LOG:  AUDIT: SESSION,1,1,WRITE,UPDATE,TABLE,public._pgaudit_probe,"
                "/* gobby-pgaudit-probe-fixed */ UPDATE\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(postgres_cli_module, "_postgres_connection", lambda: fake_conn)
    monkeypatch.setattr(postgres_cli_module, "_extension_present", lambda _conn, _ext: True)
    monkeypatch.setattr(postgres_cli_module, "_preload_libraries", lambda _conn: ["pgaudit"])
    monkeypatch.setattr(postgres_cli_module.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    monkeypatch.setattr(postgres_cli_module.subprocess, "run", _run)

    result = postgres_cli_module._probe_pgaudit_or_fail()

    assert fake_conn.committed is True
    assert any("/* gobby-pgaudit-probe-fixed */" in query for query in fake_conn.queries)
    assert commands
    assert result["audit_file"] == "/var/log/pgaudit/pgaudit.log"
    assert "gobby-pgaudit-probe-fixed" in result["audit_readback"]


def test_docker_pgaudit_log_probe_fails_when_audit_line_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module

    def _run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no audit line")

    monkeypatch.setattr(postgres_cli_module.subprocess, "run", _run)

    with pytest.raises(click.ClickException, match="pgAudit log readback probe failed"):
        postgres_cli_module._probe_docker_pgaudit_log_or_fail("missing-token")


def test_postgres_activate_native_external_requires_one_capture_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path, mode="native", hub_backend="sqlite")
    _allow_activation(monkeypatch, postgres_cli_module, tmp_path=tmp_path, mode="native")

    result = CliRunner().invoke(postgres_cli, ["activate"])

    assert result.exit_code != 0
    assert "requires exactly one of --capture-sink or --accept-no-rollback-risk" in result.output


def test_postgres_activate_restores_bootstrap_when_ticket_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path, mode="docker", hub_backend="sqlite")
    _allow_activation(monkeypatch, postgres_cli_module, tmp_path=tmp_path, mode="docker")
    monkeypatch.setattr(
        postgres_cli_module,
        "_probe_pgaudit_or_fail",
        lambda: {"write_probe": "ok"},
    )

    def _fail_write(_ticket: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(postgres_cli_module, "_write_cutover_ticket", _fail_write)

    result = CliRunner().invoke(postgres_cli, ["activate"])

    assert result.exit_code != 0
    assert isinstance(result.exception, OSError)
    assert _read_bootstrap(tmp_path)["hub_backend"] == "sqlite"


def test_postgres_deactivate_refuses_to_reenable_sqlite_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path, mode="docker", hub_backend="postgres")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)

    result = CliRunner().invoke(postgres_cli, ["deactivate"])

    assert result.exit_code != 0
    assert _read_bootstrap(tmp_path)["hub_backend"] == "postgres"
    assert not list(tmp_path.glob("bootstrap.yaml.*.bak"))
    assert "PostgreSQL deactivation to SQLite is no longer supported" in result.output
    assert "hub_backend=sqlite cannot start" in result.output


def _write_postgres_bootstrap(
    home: Path,
    *,
    mode: str,
    hub_backend: str,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "bootstrap.yaml").write_text(
        yaml.safe_dump(
            {
                "hub_backend": hub_backend,
                "database_url": "postgresql://gobby:secret@example.com/gobby",
                "postgres_install_mode": mode,
            }
        ),
        encoding="utf-8",
    )


def _read_bootstrap(home: Path) -> dict[str, Any]:
    data = yaml.safe_load((home / "bootstrap.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _allow_activation(
    monkeypatch: pytest.MonkeyPatch,
    postgres_cli_module: Any,
    *,
    tmp_path: Path,
    mode: str,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)
    monkeypatch.setattr(postgres_cli_module, "_postgres_migration_complete", lambda: True)
    monkeypatch.setattr(
        postgres_cli_module,
        "_active_install_mode",
        lambda **_kwargs: mode,
    )
