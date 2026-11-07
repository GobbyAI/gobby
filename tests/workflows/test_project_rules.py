"""This repository's project rules follow the generic rule contract."""

import ast
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from gobby.workflows.engine.command_matching import command_patterns_match
from gobby.workflows.rule_validation_yaml import validate_rule_yaml
from gobby.workflows.time_windows import time_in_windows

pytestmark = pytest.mark.unit

PROJECT_RULES = Path(__file__).resolve().parents[2] / ".gobby" / "workflows" / "rules"
QUIET_HOURS = PROJECT_RULES / "quiet-hours.yaml"
CHICAGO = ZoneInfo("America/Chicago")


@pytest.mark.filterwarnings("error::SyntaxWarning")
@pytest.mark.parametrize("yaml_file", sorted(PROJECT_RULES.glob("*.yaml")), ids=lambda p: p.name)
def test_project_rule_yaml_and_engine_dry_run(yaml_file: Path) -> None:
    reports = validate_rule_yaml(yaml_file.read_text(), source=str(yaml_file))
    assert reports, f"No rules discovered in {yaml_file}"
    for report in reports:
        report.raise_for_errors()


def _quiet_hours_rule() -> dict[str, Any]:
    document = yaml.safe_load(QUIET_HOURS.read_text())
    rules = document["rules"]
    assert len(rules) == 1
    return next(iter(rules.values()))


def _declared_windows() -> list[dict[str, Any]]:
    call = ast.parse(_quiet_hours_rule()["when"].strip(), mode="eval").body
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "time_in_windows"
    windows: list[dict[str, Any]] = ast.literal_eval(call.args[0])
    return windows


@pytest.mark.parametrize(
    ("local", "inside"),
    [
        pytest.param(datetime(2026, 11, 4, 4, 30, tzinfo=CHICAGO), True, id="daily-window"),
        pytest.param(datetime(2026, 11, 4, 6, 29, tzinfo=CHICAGO), True, id="daily-late"),
        pytest.param(datetime(2026, 11, 4, 6, 30, tzinfo=CHICAGO), False, id="after-daily"),
        pytest.param(datetime(2026, 11, 2, 6, 0, tzinfo=CHICAGO), True, id="monday-same-window"),
        pytest.param(datetime(2026, 11, 2, 6, 30, tzinfo=CHICAGO), False, id="after-monday"),
        pytest.param(datetime(2026, 11, 4, 3, 59, tzinfo=CHICAGO), False, id="before-window"),
    ],
)
def test_quiet_hours_rule_declares_policy_windows(local: datetime, inside: bool) -> None:
    assert time_in_windows(_declared_windows(), now=local) is inside


def _blocks(command: str) -> bool:
    effects = _quiet_hours_rule()["effects"]
    return any(
        effect["type"] == "block"
        and "Bash" in effect["tools"]
        and command_patterns_match(
            command,
            pattern=effect["command_pattern"],
            mask_quoted=effect.get("mask_quoted", False),
        )
        for effect in effects
    )


@pytest.mark.parametrize(
    "command",
    [
        "gobby restart",
        "uv run gobby cutover --allow-dirty",
        "cd /repo && gobby stop",
        "gobby start --verbose",
        "uv run python -m gobby.runner",
        "python3 -m gobby.runner --verbose",
        "curl -X POST http://127.0.0.1:60887/api/admin/restart",
        "curl -s -X POST localhost:60887/api/admin/shutdown?restart_supervisor=true",
        "bash -c 'gobby restart'",
        'sh -c "uv run gobby cutover"',
        "zsh -lc 'cd /repo && gobby stop'",
    ],
)
def test_quiet_hours_rule_blocks_daemon_management(command: str) -> None:
    assert _blocks(command)


@pytest.mark.parametrize(
    "command",
    [
        "gobby status",
        "uv run gobby tasks list",
        "git commit -m 'gobby restart notes'",
        "curl -s http://127.0.0.1:60887/api/admin/status",
        "uv run pytest tests/test_runner_shutdown.py",
    ],
)
def test_quiet_hours_rule_allows_other_commands(command: str) -> None:
    assert not _blocks(command)
