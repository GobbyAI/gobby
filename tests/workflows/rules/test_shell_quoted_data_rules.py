"""Single-quoted JSON is literal data for the Bash rules that share the shell classifiers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks._normalization_tools import _set_canonical_tool_metadata
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "55555555-5555-4555-8555-555555555555"
GOBBY_PROJECT_ID = "d45545c5-ded5-4335-b115-0245752edacf"

GCODE_EVIDENCE_READ = (
    "gcode evidence --request-json '"
    '{"schema_version":1,"operation":"read","read":'
    '{"kind":"commit_metadata","commit_oid":"243d8db7f96f1b421fae75c95ff105c2cdcefe16"},'
    '"max_bytes":100000}'
    "'"
)

# Each rule that reads the same shell classifiers, with one real refusal it must keep making.
RULE_REFUSALS = {
    "block-direct-provider-launch": "claude -p hi",
    "block-unresolved-scope-shell-write": 'printf x > "$TARGET"',
    "block-gobby-command-word-expansion": "gobby {tasks,sessions} list",
    "require-task-before-edit": "bash <<'EOF'\ntouch owned.py\nEOF",
}


def _bash_event(command: str, worktree: Path) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command, "workdir": str(worktree)},
        "project_path": str(worktree),
    }
    _set_canonical_tool_metadata(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=GOBBY_PROJECT_ID,
        data=data,
    )


async def _decide(temp_db: HubDatabase, worktree: Path, rule: str, command: str) -> tuple[str, str]:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("DELETE FROM rule_definitions WHERE name <> %s", (rule,))
    response = await RuleEngine(temp_db).evaluate(
        _bash_event(command, worktree),
        session_id=SESSION_ID,
        variables={"require_task_before_edit": True, "task_claimed": False},
    )
    return response.decision, response.reason or ""


@pytest.mark.parametrize("rule", list(RULE_REFUSALS))
async def test_single_quoted_gcode_evidence_json_is_allowed(
    temp_db: HubDatabase, tmp_path: Path, rule: str
) -> None:
    decision, reason = await _decide(temp_db, tmp_path, rule, GCODE_EVIDENCE_READ)

    assert decision == "allow", reason


@pytest.mark.parametrize(("rule", "command"), list(RULE_REFUSALS.items()))
async def test_each_rule_keeps_its_real_refusal(
    temp_db: HubDatabase, tmp_path: Path, rule: str, command: str
) -> None:
    decision, _ = await _decide(temp_db, tmp_path, rule, command)

    assert decision == "block"
