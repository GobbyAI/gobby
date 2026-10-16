"""Quote-aware Bash command_pattern matching for bundled full-suite rules.

Quoted ``;``, ``&``, ``|``, ``(``, and backticks are data, not simple-command
starts. ``command_not_pattern`` still sees quoted test paths.
"""

from __future__ import annotations

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.command_matching import command_patterns_match
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

FULL_SUITE_RULES = (
    "no-full-pytest-suite",
    "no-full-vitest-suite",
    "no-full-cargo-test",
    "no-full-go-test",
)

GCODE_GREP_SINGLE = "gcode grep -l -E 'pytest|vitest|cargo test|go test' src"
GCODE_GREP_DOUBLE = 'gcode grep -l -E "pytest|vitest|cargo test|go test" src'


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")


def _get_rule(manager: RuleDefinitionManager, name: str) -> RuleDefinitionBody:
    row = manager.get_by_name(name)
    assert row is not None, f"Rule {name!r} not found after sync"
    return RuleDefinitionBody.model_validate(row.definition_json)


def _blocks(body: RuleDefinitionBody, command: str) -> bool:
    return any(
        command_patterns_match(
            command,
            pattern=effect.command_pattern,
            not_pattern=effect.command_not_pattern,
            mask_quoted=effect.mask_quoted,
        )
        for effect in body.resolved_effects
        if effect.type == "block" and effect.command_pattern is not None
    )


def test_quoted_separator_is_not_command_boundary(
    db: HubDatabase, manager: RuleDefinitionManager
) -> None:
    _sync_bundled(db)
    pytest_rule = _get_rule(manager, "no-full-pytest-suite")
    vitest_rule = _get_rule(manager, "no-full-vitest-suite")
    cargo_rule = _get_rule(manager, "no-full-cargo-test")

    for command in (GCODE_GREP_SINGLE, GCODE_GREP_DOUBLE):
        for name in FULL_SUITE_RULES:
            assert not _blocks(_get_rule(manager, name), command), (
                f"{name} must allow quoted-separator search: {command}"
            )

    assert not _blocks(
        pytest_rule,
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_command_matching.py",
    )
    assert not _blocks(pytest_rule, "pytest 'tests/x/test_y.py'")

    assert _blocks(pytest_rule, "pytest")
    assert _blocks(pytest_rule, "echo x | pytest")
    assert _blocks(cargo_rule, "x; cargo test")
    assert _blocks(vitest_rule, "cd web && npx vitest")


# The #24561 reproductions: an escaped backtick inside a double-quoted search
# pattern is literal, so the whole pattern stays string data.
ESCAPED_BACKTICK_GREPS = (
    r'gcode grep -n -E "uv run pytest <file>|as \`uv run pytest x\`|^- 2\.2\.6" plan.md -m 10',
    r'gcode grep -n -E "pytest <file>|pytest x\`\.|^- 2\.2\.6" plan.md -m 10',
    r'grep -E "a|uv run pytest \$(x)" notes.md',
)


@pytest.mark.parametrize("name", ["no-full-pytest-suite", "require-pytest-guard-env"])
@pytest.mark.parametrize("command", ESCAPED_BACKTICK_GREPS)
def test_escaped_substitution_in_double_quotes_is_data(
    db: HubDatabase, manager: RuleDefinitionManager, name: str, command: str
) -> None:
    _sync_bundled(db)
    assert not _blocks(_get_rule(manager, name), command)


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("no-full-pytest-suite", "uv run pytest"),
        ("no-full-pytest-suite", 'echo "$(uv run pytest)"'),
        ("no-full-pytest-suite", 'echo "`uv run pytest`"'),
        # An escaped backtick runs once a backquote body, `eval` or `bash -c` decodes it.
        ("no-full-pytest-suite", 'echo `echo "\\`uv run pytest\\`"`'),
        ("no-full-pytest-suite", 'eval "echo \\`uv run pytest\\`"'),
        ("no-full-pytest-suite", 'bash -c "echo \\`uv run pytest\\`"'),
        ("require-pytest-guard-env", "uv run pytest tests/x/test_y.py"),
        ("require-pytest-guard-env", 'echo "\\` $(uv run pytest tests/x/test_y.py)"'),
    ],
)
def test_live_pytest_still_blocks(
    db: HubDatabase, manager: RuleDefinitionManager, name: str, command: str
) -> None:
    _sync_bundled(db)
    assert _blocks(_get_rule(manager, name), command)
