"""CLI wiring tests for the PostgreSQL command group."""

from __future__ import annotations

from typing import Any

import pytest
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
