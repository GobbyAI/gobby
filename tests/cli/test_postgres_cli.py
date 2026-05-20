"""CLI wiring tests for the PostgreSQL command group."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    monkeypatch.setattr(
        postgres_cli_module,
        "_probe_pgaudit_or_fail",
        lambda: {"write_probe": "ok"},
    )

    result = CliRunner().invoke(postgres_cli, ["activate"])

    assert result.exit_code == 0
    assert _read_bootstrap(tmp_path)["hub_backend"] == "postgres"
    assert list(tmp_path.glob("bootstrap.yaml.*.bak"))
    tickets = list((tmp_path / "migrations").glob("cutover-*.json"))
    assert len(tickets) == 1
    payload = json.loads(tickets[0].read_text(encoding="utf-8"))
    assert payload["mode"] == "docker"
    assert payload["capture_kind"] == "pgaudit-managed"
    assert payload["capture_value"] is None
    assert payload["verification"]["state"] == "ok"
    assert "_path" not in payload
    assert "gobby stop && gobby postgres deactivate && gobby start" in result.output
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
    monkeypatch.setattr(
        postgres_cli_module,
        "_require_ownership_sentinel_or_fail",
        lambda: ownership_checks.append("checked"),
    )

    result = CliRunner().invoke(
        postgres_cli,
        ["activate", "--capture-sink", f"pgaudit-file:{tmp_path / 'pgaudit.log'}"],
    )

    assert result.exit_code == 0
    assert ownership_checks == ["checked"]
    assert _read_bootstrap(tmp_path)["hub_backend"] == "postgres"
    [ticket_path] = list((tmp_path / "migrations").glob("cutover-*.json"))
    payload = json.loads(ticket_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "external"
    assert payload["capture_kind"] == "pgaudit-file"
    assert payload["capture_value"] == str(tmp_path / "pgaudit.log")


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


def test_postgres_deactivate_flips_bootstrap_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path, mode="docker", hub_backend="postgres")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)

    result = CliRunner().invoke(postgres_cli, ["deactivate"])

    assert result.exit_code == 0
    assert _read_bootstrap(tmp_path)["hub_backend"] == "sqlite"
    assert list(tmp_path.glob("bootstrap.yaml.*.bak"))
    assert "hub_backend set to sqlite." in result.output


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
