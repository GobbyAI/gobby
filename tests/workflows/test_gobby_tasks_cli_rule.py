"""Tests for the rules-engine guard around native task CLI mutations."""

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


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    sync_bundled_rules(database, get_bundled_rules_path())
    database.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return database


@pytest.fixture
def effect(db: HubDatabase) -> RuleEffect:
    manager = RuleDefinitionManager(db)
    row = manager.get_by_name("block-gobby-tasks-cli")
    assert row is not None

    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.event.value == "before_tool"
    assert body.resolved_effects[0].type == "block"
    return body.resolved_effects[0]


def _shell_event(tool_name: str, command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": tool_name, "tool_input": {"command": command}},
    )


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        "uv run gobby tasks create --help",
        "gobby tasks update #1 --priority 1",
        "gobby tasks close #1",
        "gobby tasks claim #1",
    ],
)
def test_blocks_mutating_task_cli_commands(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        "uv run gobby tasks list --ready",
        "gobby tasks ready",
        "gobby tasks blocked",
        "gobby tasks stats",
        "gobby tasks show #1",
        "gobby tasks backup --quiet",
        "gobby tasks expand validate-plan .gobby/plans/example.md",
        "gobby tasks validation_history #1",
        "gobby tasks doctor",
    ],
)
def test_allows_read_only_task_cli_commands(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        # A quoted heredoc body is stdin data, not an invocation (#23134): the
        # phrase can sit at the start of a line inside a serialized string.
        'RTK_DISABLED=1 uv run python - <<\'PY\'\nDOC = """\ngobby tasks close\n"""\nPY',
        # A serialized evidence/documentation payload is data, not an invocation.
        'RTK_DISABLED=1 uv run python - <<\'PY\'\nPAYLOAD = \'{"content": "gobby tasks close", "task": "#1"}\'\nPY',
        # A quoted echo argument is prose.
        'echo "gobby tasks close 42"',
        # A wrapper keeps its command's word boundaries: the quoted -c source
        # is one data argument, not a `;`-separated invocation.
        'timeout 5 python -c "x = 1; gobby tasks close 42"',
        'uv run --with pyyaml python -c "x = 1; gobby tasks close 42"',
    ],
)
def test_allows_quoted_heredoc_and_string_data(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        # An unquoted interpreter heredoc body executes: keep blocking.
        "uv run python - <<PY\ngobby tasks close 1\nPY",
        # A quoted heredoc piped to a shell executes: keep blocking.
        "bash -s <<EOF\ngobby tasks close 1\nEOF",
        # Command substitution inside double quotes still executes.
        'echo "$(gobby tasks close 42)"',
        # Known fail-closed residual: a backtick inside double quotes keeps the
        # span visible (it may execute), so this quoted documentation line still
        # blocks. Narrower than the reported false positive; kept fail-closed.
        "RTK_DISABLED=1 uv run python - <<'PY'\nDOC = \"use `gobby tasks close` here\"\nPY",
    ],
)
def test_blocks_executed_heredoc_and_substitution(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


# Assembled by concatenation so this test module is never itself an invocation
# the guard would match in command position.
_MUTATION = "gobby " + "tasks close 1"


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        # A `#` comment ends at the newline, so the next line is a real
        # invocation. A comment-unaware masked pass reads the apostrophe as an
        # unterminated single-quote span and blanks that invocation (#23134).
        "# don't\n" + _MUTATION,
        "echo hi # it's\n" + _MUTATION,
        "bash <<'EOF'\n# don't\n" + _MUTATION + "\nEOF",
        "bash <<'EOF'\necho hi  # can't\n" + _MUTATION + "\nEOF",
        "zsh <<'EOF'\n# won't\n" + _MUTATION + "\nEOF",
    ],
)
def test_blocks_command_after_apostrophe_comment(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        # In ANSI-C quoting `\'` is an escaped apostrophe, not the closing
        # quote; misreading it opens a span that blanks the real invocation.
        "echo $'it\\'s'; " + _MUTATION + "; echo 'z'",
        "echo $'it\\'s'\n" + _MUTATION + "\necho 'z'",
        "echo $'x\\'y' | xargs " + _MUTATION + " 'z'",
        "echo \"$(echo $'a\\'b'; " + _MUTATION + ')"',
    ],
)
def test_blocks_command_after_ansi_c_escaped_quote(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo $'it\\'s " + _MUTATION + "'",
        # A decoded newline inside an echo argument is still data.
        "echo $'a\\n" + _MUTATION + "'",
        # bash stops at the unterminated quote the decoded script opens; nothing runs.
        "bash -c $'echo it\\'s; " + _MUTATION + "'",
        # `\c\'` keeps the apostrophe inside the quote, so the rest is echo data.
        "echo $'\\c\\'; " + _MUTATION + " #'",
    ],
)
def test_allows_ansi_c_quoted_data(db: HubDatabase, effect: RuleEffect, command: str) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize("tool_name", ["Bash", "exec_command"])
@pytest.mark.parametrize(
    "command",
    [
        'bash -c "' + _MUTATION + '"',
        "bash -c '" + _MUTATION + "'",
        "sh -c 'cd /x && " + _MUTATION + "'",
        'eval "' + _MUTATION + '"',
        "eval '" + _MUTATION + "'",
        "echo 1 | xargs -I{} " + _MUTATION,
        "timeout 5 " + _MUTATION,
        "watch -n1 '" + _MUTATION + "'",
        "ssh host '" + _MUTATION + "'",
        # watch and ssh join every remaining word into the command they run.
        "watch -n1 " + _MUTATION,
        "watch -n 5 -d " + _MUTATION,
        "ssh host " + _MUTATION,
        "ssh -p 22 host " + _MUTATION,
        # `--` ends the shell's options; the script is the word after it.
        "sh -c -- '" + _MUTATION + "'",
        "uv run --with pyyaml " + _MUTATION,
        "uv run --project . --frozen " + _MUTATION,
        # bash decodes C escapes inside `$'...'`, so each spells a separator or name.
        "bash -c $'echo hi\\n" + _MUTATION + "'",
        "bash -c $'true;\\t" + _MUTATION + "'",
        "bash -c $'echo hi\\x0a" + _MUTATION + "'",
        "bash -c $'echo hi\\012" + _MUTATION + "'",
        "bash -c $'echo hi\\cJ" + _MUTATION + "'",
        "bash -c $'echo hi\\u000a" + _MUTATION + "'",
        "bash -c $'\\x67" + _MUTATION[1:] + "'",
        # zsh keeps a decoded NUL, so `eval` still runs the command after it.
        "eval $'true\\0; " + _MUTATION + "'",
        "eval $'true\\x00; " + _MUTATION + "'",
        "eval $'true\\c@; " + _MUTATION + "'",
    ],
)
def test_blocks_wrapped_mutating_script(
    db: HubDatabase, effect: RuleEffect, tool_name: str, command: str
) -> None:
    event = _shell_event(tool_name, command)

    assert RuleEngine(db)._should_block(effect, event) is True


@pytest.mark.parametrize(
    "command",
    [
        # An apostrophe comment no longer blinds the masker; the data payload
        # stays data.
        "uv run python - <<'PY'\n# it's\nDOC = \"\"\"\n" + _MUTATION + '\n"""\nPY',
        "uv run python - <<'PY'\n# don't\nPAYLOAD = '{\"content\": \"" + _MUTATION + "\"}'\nPY",
    ],
)
def test_allows_apostrophe_comment_before_data_payload(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is False


@pytest.mark.parametrize(
    "command",
    [
        # Wrapper scripts nest; every level must resolve, not just the first.
        "bash -c \"bash -c '" + _MUTATION + "'\"",
        "eval \"bash -c '" + _MUTATION + "'\"",
        "sh -c \"eval '" + _MUTATION + "'\"",
    ],
)
def test_blocks_nested_wrapped_mutating_script(
    db: HubDatabase, effect: RuleEffect, command: str
) -> None:
    event = _shell_event("Bash", command)

    assert RuleEngine(db)._should_block(effect, event) is True
