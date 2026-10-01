from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

from gobby.cli import cli as root_cli

pytestmark = pytest.mark.unit
schema_module = importlib.import_module("gobby.cli.schema")


def test_schema_command_is_registered_on_root_cli() -> None:
    assert "schema" in root_cli.commands


def test_apply_schema_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """`schema apply` applies the plain chain; no destructive branch or campaign remains."""
    required: list[str] = []

    class Runtime:
        def require_database(self) -> None:
            required.append("database")

    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda _ctx: Runtime())
    monkeypatch.setattr(schema_module, "installed_schema_version", lambda: 456)

    applied = CliRunner().invoke(root_cli, ["schema", "apply"])
    rejected = CliRunner().invoke(root_cli, ["schema", "apply", "--destructive"])
    campaign = CliRunner().invoke(root_cli, ["hub-maintenance", "run", "schema-apply"])

    assert applied.exit_code == 0, applied.output
    assert required == ["database"]
    assert "Schema is at version 456" in applied.output
    assert rejected.exit_code == 2
    assert "No such option '--destructive'" in rejected.output
    assert campaign.exit_code == 2
    assert "'schema-apply' is not one of" in campaign.output
