"""Bundled Bash rules see through command prefixes that still run the command (#23134)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

# Each prefix runs the command placed at `{}`.
_PREFIXES = [
    "time {}",
    "time -p {}",
    "env {}",
    "env FOO=1 {}",
    "env -u HOME {}",
    "nice {}",
    "nice -n 5 {}",
    "nohup {}",
    "timeout 30 {}",
    "timeout -s KILL 30 {}",
    "exec {}",
    "builtin {}",
    "sudo -u root {}",
    "echo x | xargs {}",
    "echo x | xargs -n 1 {}",
    "{{ {}; }}",
    "(time {})",
    "( time {} )",
    "time env FOO=1 nice -n 5 {}",
]

_RULE_COMMANDS = [
    ("no-push", "git push"),
    ("no-push", "git push origin main"),
    ("no-full-vitest-suite", "npx vitest related"),
    ("no-full-pytest-suite", "uv run pytest"),
    ("no-recursive-rm", "rm -rf /tmp/x"),
    ("no-daemon-management", "gobby restart"),
]


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return temp_db


def _effect(db: HubDatabase, rule_name: str) -> RuleEffect:
    row = RuleDefinitionManager(db).get_by_name(rule_name)
    assert row is not None
    body = RuleDefinitionBody.model_validate(row.definition_json)
    return next(effect for effect in body.resolved_effects if effect.type == "block")


def _bash_event(command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": command}},
    )


@pytest.mark.parametrize(("rule_name", "command"), _RULE_COMMANDS)
def test_rule_blocks_its_bare_command(db: HubDatabase, rule_name: str, command: str) -> None:
    assert RuleEngine(db)._should_block(_effect(db, rule_name), _bash_event(command)) is True


@pytest.mark.parametrize("prefix", _PREFIXES)
@pytest.mark.parametrize(("rule_name", "command"), _RULE_COMMANDS)
def test_rule_blocks_its_command_behind_a_running_prefix(
    db: HubDatabase, rule_name: str, command: str, prefix: str
) -> None:
    event = _bash_event(prefix.format(command))

    assert RuleEngine(db)._should_block(_effect(db, rule_name), event) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo time git push",
        'git commit -m "time git push"',
        "command -v git push",
        "time git status",
    ],
)
def test_no_push_allows_a_prefix_that_runs_no_push(db: HubDatabase, command: str) -> None:
    assert RuleEngine(db)._should_block(_effect(db, "no-push"), _bash_event(command)) is False


@pytest.mark.parametrize(
    "command",
    ["time pip install requests", "env FOO=1 python -m pip install requests", "(time pip3 list)"],
)
def test_require_uv_blocks_bare_pip_behind_a_prefix(db: HubDatabase, command: str) -> None:
    assert RuleEngine(db)._should_block(_effect(db, "require-uv"), _bash_event(command)) is True


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m pip install requests",
        "time uv run python -m pip install requests",
        "uv run --with pyyaml pip list",
    ],
)
def test_require_uv_allows_pip_under_uv_run(db: HubDatabase, command: str) -> None:
    assert RuleEngine(db)._should_block(_effect(db, "require-uv"), _bash_event(command)) is False


# A full-suite exemption reads every command subject joined by newlines, so a
# later command's path must not lend a bare run its target (#23587).
@pytest.mark.parametrize(
    ("rule_name", "command"),
    [
        ("no-full-pytest-suite", "uv run pytest; ./x.py"),
        ("no-full-pytest-suite", "uv run pytest; tests/run.sh"),
        ("no-full-vitest-suite", "npx vitest; ls a.test.ts"),
        ("no-full-vitest-suite", "npx vitest related; ls"),
        ("no-full-cargo-test", "cargo test; ls"),
    ],
)
def test_full_suite_run_takes_no_target_from_a_later_command(
    db: HubDatabase, rule_name: str, command: str
) -> None:
    assert RuleEngine(db)._should_block(_effect(db, rule_name), _bash_event(command)) is True


@pytest.mark.parametrize(
    ("rule_name", "command"),
    [
        ("no-full-pytest-suite", "uv run pytest tests/x.py; ls"),
        ("no-full-vitest-suite", "npx vitest related src/a.ts; ls"),
        ("no-full-vitest-suite", "(npx vitest related src/a.ts)"),
        ("no-full-cargo-test", "cargo test -p gobby-core; ls"),
    ],
)
def test_targeted_run_stays_exempt_beside_another_command(
    db: HubDatabase, rule_name: str, command: str
) -> None:
    assert RuleEngine(db)._should_block(_effect(db, rule_name), _bash_event(command)) is False
