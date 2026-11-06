"""A bundled command rule that refuses a plain daemon command refuses its env-prefixed form too."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import gobby

pytestmark = pytest.mark.unit

RULES_ROOT = Path(gobby.__file__).parent / "install" / "shared" / "workflows" / "rules"

PLAIN_DAEMON_COMMANDS = [
    "uv run gobby restart --wait",
    "uv run gobby cutover --wait",
    "uv run gobby restart --force",
    "gobby cutover --allow-dirty",
    "gobby stop",
    "gobby start",
    "curl -X POST http://127.0.0.1:60887/api/admin/restart",
]

ENV_PREFIXES = [
    "env -u GOBBY_AGENT_RUN_ID -u GOBBY_MANAGED_EXECUTION_ID -u GOBBY_MANAGED_EXECUTION_BOOTSTRAP ",
    "env -i ",
    "env - ",
    "/usr/bin/env --unset=GOBBY_AGENT_RUN_ID ",
    "env --unset GOBBY_MANAGED_EXECUTION_ID -- ",
    "env GOBBY_EXTRA=1 PATH=/usr/bin ",
]


def _bundled_command_patterns() -> list[tuple[str, str]]:
    patterns: list[tuple[str, str]] = []
    for path in sorted(RULES_ROOT.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        for rule_name, rule in (data.get("rules") or {}).items():
            for effect in rule.get("effects") or []:
                pattern = effect.get("command_pattern")
                if isinstance(pattern, str):
                    patterns.append((f"{path.name}::{rule_name}", pattern))
    return patterns


COMMAND_PATTERNS = _bundled_command_patterns()


@pytest.mark.parametrize("command", PLAIN_DAEMON_COMMANDS)
def test_some_bundled_rule_refuses_the_plain_daemon_command(command: str) -> None:
    assert any(re.search(pattern, command) for _, pattern in COMMAND_PATTERNS)


@pytest.mark.parametrize(("rule_id", "pattern"), COMMAND_PATTERNS)
def test_env_prefixed_daemon_command_matches_wherever_the_plain_one_does(
    rule_id: str, pattern: str
) -> None:
    compiled = re.compile(pattern)
    for command in PLAIN_DAEMON_COMMANDS:
        if not compiled.search(command):
            continue
        for prefix in ENV_PREFIXES:
            assert compiled.search(prefix + command), (rule_id, prefix + command)
