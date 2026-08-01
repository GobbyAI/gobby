"""CLI wiring tests for the PostgreSQL command group."""

from __future__ import annotations

from pathlib import Path
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
    assert {"install", "backup", "restore", "status"} <= set(postgres_group.commands)
    assert "activate" not in postgres_group.commands
    assert "uninstall" not in postgres_group.commands
    assert "migrate-from-postgres" not in postgres_group.commands
    assert "deactivate" not in postgres_group.commands


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["postgres", "install", "--help"], []),
        (["postgres", "status", "--help"], ["--json"]),
        (["postgres", "backup", "--help"], ["--output"]),
        (["postgres", "restore", "--help"], ["--clean", "--yes", "--allow-unverified"]),
    ],
)
def test_postgres_cli_help_exposes_phase1_options(args: list[str], expected: list[str]) -> None:
    from gobby.cli import cli

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0
    for text in expected:
        assert text in result.output


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
            "database_url": "postgresql://gobby:secret@localhost:60891/gobby",
            "message": "PostgreSQL configured",
        }

    monkeypatch.setattr(postgres_cli_module, "install_postgres", _install_postgres)

    result = CliRunner().invoke(postgres_cli, ["install"])

    assert result.exit_code == 0
    assert calls == [{}]
    assert "PostgreSQL configured" in result.output


@pytest.mark.parametrize("removed_option", ["--mode", "--dsn"])
def test_postgres_install_command_rejects_removed_options(removed_option: str) -> None:
    from gobby.cli.postgres import postgres_cli

    result = CliRunner().invoke(postgres_cli, ["install", removed_option, "value"])

    assert result.exit_code != 0
    assert f"No such option '{removed_option}'" in result.output


def test_postgres_status_json_emits_structured_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    async def _status() -> dict[str, Any]:
        return {
            "dsn_host": "localhost",
            "dsn_db": "gobby",
            "healthy": True,
            "extensions": {"pg_search": True, "pgaudit": True, "pgcrypto": True},
            "preload_libraries": ["pg_search", "pgaudit"],
        }

    monkeypatch.setattr(postgres_cli_module, "get_postgres_status", _status)

    result = CliRunner().invoke(postgres_cli, ["status", "--json"])

    assert result.exit_code == 0
    assert '"pg_search": true' in result.output


def test_postgres_backup_command_refuses_when_daemon_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: True)

    result = CliRunner().invoke(postgres_cli, ["backup"])

    assert result.exit_code != 0
    assert "Stop the daemon first: gobby stop" in result.output


def test_postgres_backup_command_invokes_verified_backup_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    calls: list[dict[str, Any]] = []

    def _backup(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "backup_dir": str(tmp_path / "backup"),
            "dump_path": str(tmp_path / "backup/gobby.dump"),
            "metadata_path": str(tmp_path / "backup/metadata.json"),
            "sha256s_path": str(tmp_path / "backup/SHA256SUMS"),
            "dump_sha256": "a" * 64,
            "verified": True,
            "sha256_verified": True,
        }

    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)
    monkeypatch.setattr(postgres_cli_module, "get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr(postgres_cli_module, "create_postgres_backup", _backup)

    result = CliRunner().invoke(postgres_cli, ["backup", "--output", str(tmp_path / "backup")])

    assert result.exit_code == 0
    assert calls == [{"output_dir": tmp_path / "backup", "gobby_home": tmp_path}]
    assert "PostgreSQL backup created:" in result.output
    assert "Verified: pg_restore --list" in result.output
    assert "SHA256SUMS:" in result.output
    assert "Verified: SHA256SUMS" in result.output


def test_postgres_restore_command_refuses_when_daemon_is_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    dump = tmp_path / "gobby.dump"
    dump.write_bytes(b"dump")
    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: True)

    result = CliRunner().invoke(postgres_cli, ["restore", str(dump), "--yes"])

    assert result.exit_code != 0
    assert "Stop the daemon first: gobby stop" in result.output


def test_postgres_restore_command_invokes_restore_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    dump = tmp_path / "gobby.dump"
    dump.write_bytes(b"dump")
    calls: list[dict[str, Any]] = []

    def _restore(source: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append({"source": source, **kwargs})
        return {
            "database_url": "postgresql://gobby:****@localhost:60891/gobby",
            "dump_sha256": "b" * 64,
            "sha256_verified": True,
            "probes": {
                "pg_search_present": True,
                "pgaudit_present": True,
                "pgcrypto_present": True,
            },
        }

    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)
    monkeypatch.setattr(postgres_cli_module, "get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr(postgres_cli_module, "restore_postgres_backup", _restore)

    result = CliRunner().invoke(
        postgres_cli,
        ["restore", str(dump), "--clean", "--allow-unverified", "--yes"],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "source": dump,
            "clean": True,
            "allow_unverified": True,
            "gobby_home": tmp_path,
        }
    ]
    assert "PostgreSQL restore completed." in result.output
    assert "Verified: SHA256SUMS" in result.output
    assert "pg_search: yes" in result.output
    assert "pgcrypto:  yes" in result.output


def test_postgres_restore_command_decline_skips_restore_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres as postgres_cli_module
    from gobby.cli.postgres import postgres_cli

    dump = tmp_path / "gobby.dump"
    dump.write_bytes(b"dump")
    calls: list[Path] = []

    def _restore(source: Path, **_kwargs: Any) -> dict[str, Any]:
        calls.append(source)
        raise AssertionError("restore helper should not be called")

    monkeypatch.setattr(postgres_cli_module, "_daemon_running", lambda: False)
    monkeypatch.setattr(postgres_cli_module, "restore_postgres_backup", _restore)

    result = CliRunner().invoke(postgres_cli, ["restore", str(dump)], input="n\n")

    assert result.exit_code == 0
    assert calls == []
    assert "Aborted." in result.output


def test_postgres_restore_rejects_missing_checksum_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import gobby.cli.postgres_backup as backup_module

    dump = tmp_path / "gobby.dump"
    dump.write_bytes(b"dump")
    monkeypatch.setattr(
        backup_module,
        "_resolve_database_url",
        lambda _home: "postgresql://gobby:secret@localhost:5432/gobby",
    )
    monkeypatch.setattr(backup_module, "_require_managed_docker_postgres", lambda **_kwargs: None)

    with pytest.raises(click.ClickException, match="missing trusted checksum sidecar"):
        backup_module.restore_postgres_backup(dump, gobby_home=tmp_path)


def test_postgres_uninstall_command_is_not_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gobby.cli.postgres import postgres_cli

    _write_postgres_bootstrap(tmp_path)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))

    result = CliRunner().invoke(postgres_cli, ["uninstall"])

    assert result.exit_code == 2
    assert "No such command 'uninstall'" in result.output
    bootstrap = _read_bootstrap(tmp_path)
    assert "hub_backend" not in bootstrap
    assert bootstrap["database_url"] == "postgresql://gobby:secret@example.com/gobby"
    assert "postgres_install_mode" not in bootstrap


def _write_postgres_bootstrap(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "bootstrap.yaml").write_text(
        yaml.safe_dump(
            {
                "database_url": "postgresql://gobby:secret@example.com/gobby",
            }
        ),
        encoding="utf-8",
    )


def _read_bootstrap(home: Path) -> dict[str, Any]:
    data = yaml.safe_load((home / "bootstrap.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data
