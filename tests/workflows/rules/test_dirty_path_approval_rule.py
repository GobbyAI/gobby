"""Approving unowned dirty paths is the operator's decision; no seat may run the approval."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

RULE_NAME = "block-dirty-path-approval-cli"
EXPANSION_RULE = "block-gobby-cli-expansion"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
GOBBY_PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"
# Every launcher, wrapper and global-option spelling that reaches the CLI.
INVOCATIONS = (
    "gobby sessions approve-dirty-paths s7 --checkout /repo a.txt",
    "cd /repo && uv run gobby sessions approve-dirty-paths s7 a.txt",
    "env gobby sessions approve-dirty-paths s7 --checkout /repo a.txt",
    "env -i PATH=/usr/bin gobby sessions approve-dirty-paths s7 a.txt",
    "uv run --directory /repo gobby sessions approve-dirty-paths s7 a.txt",
    "uv run --no-sync gobby sessions approve-dirty-paths s7 a.txt",
    "uvx gobby sessions approve-dirty-paths s7 a.txt",
    "python -m gobby.cli sessions approve-dirty-paths s7 a.txt",
    "gobby --verbose sessions approve-dirty-paths s7 a.txt",
    ".venv/bin/gobby sessions approve-dirty-paths s7 a.txt",
    "g=gobby; $g sessions approve-dirty-paths s7 a.txt",
    'gobby sessions approve-dirty-path"s" s7 a.txt',
    "bash -c 'gobby sessions approve-dirty-paths s7 a.txt'",
    'echo "$(gobby sessions approve-dirty-paths s7 a.txt)"',
    "a=approve-dirty-; b=paths; gobby sessions $a$b s7 --checkout /repo a.txt",
    "a=approve-dirty-; b=paths; gobby 'sessions' $a$b s7 a.txt",
    'a=approve-dirty-; b=paths; gobby "sessions" $a$b s7 a.txt',
    "a=approve-dirty-; b=paths; gobby se\\ssions $a$b s7 a.txt",
    "a=approve-dirty-; b=paths; gobby sess'ions' $a$b s7 a.txt",
    'a=approve-dirty-; b=paths; gobby sessions "$a$b" s7 a.txt',
    'eval "gobby sessions approve-dirty-paths s7 a.txt"',
    "sh -c 'gobby sessions $v s7 a.txt'",
    'gobby sessions list --project "$(gobby sessions approve-dirty-paths s7 a.txt)"',
    "gobby sessions list --project $P; a=approve-dirty-; b=paths; $g sessions $a$b s7 a.txt",
)
# Braces and globs that would name the approval, other gobby subcommands and quoted
# arguments. The rule leaves them to the operator route's credential check and
# never refuses them for their shape.
ALLOWED = (
    'gobby sessions show "$SID"',
    'gobby sessions {approve-dirty-path"s",s7} a.txt',
    "gobby sessions approve-dirty-p?ths s7 a.txt",
    "gobby sessions approve-dirty-p[a]ths s7 a.txt",
    "gobby tasks show $TASK",
    "gobby tasks show {a,b}",
    "gobby tasks list --status *.md",
    """gobby tasks create --data '{"title": "sessions $HOME"}'""",
)


def _bash_event(command: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=GOBBY_PROJECT_ID,
        data={"command": command, "tool_input": {"command": command}, "tool_name": "Bash"},
    )


@pytest.mark.parametrize(
    "variables",
    [{"is_spawned_agent": True}, {}],
    ids=["spawned", "interactive"],
)
async def test_dirty_path_approval_rules_match_criterion_4_scope(
    temp_db: HubDatabase, variables: dict[str, object]
) -> None:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute(
        "DELETE FROM rule_definitions WHERE name NOT IN (%s, %s)", (RULE_NAME, EXPANSION_RULE)
    )
    engine = RuleEngine(temp_db)

    async def decide(command: str) -> tuple[str, str]:
        response = await engine.evaluate(
            _bash_event(command), session_id=SESSION_ID, variables=dict(variables)
        )
        return response.decision, response.reason or ""

    direct = await decide("gobby sessions approve-dirty-paths s7 --checkout /repo a.txt")
    decisions = {
        command: (await decide(command))[0]
        for command in (
            *INVOCATIONS,
            *ALLOWED,
            'echo "ask the operator: gobby sessions approve-dirty-paths s7 a.txt"',
            "git commit -m 'relay gobby sessions approve-dirty-paths' -- a.txt",
            "gobby sessions list",
            "gobby sessions list --project $PROJECT",
            "uv run gobby sessions list --project $PROJECT",
        )
    }

    assert "operator" in direct[1]
    assert decisions == {
        **dict.fromkeys(INVOCATIONS, "block"),
        **dict.fromkeys(ALLOWED, "allow"),
        'echo "ask the operator: gobby sessions approve-dirty-paths s7 a.txt"': "allow",
        "git commit -m 'relay gobby sessions approve-dirty-paths' -- a.txt": "allow",
        "gobby sessions list": "allow",
        "gobby sessions list --project $PROJECT": "allow",
        "uv run gobby sessions list --project $PROJECT": "allow",
    }
