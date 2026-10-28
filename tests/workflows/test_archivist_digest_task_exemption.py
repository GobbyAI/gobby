"""The Archivist's dated digest is a taskless role duty, so editing it needs no claim.

`require-task-before-edit` gates repository writes on a claimed task. The digest at
`<project>/.gobby/reports/gobby-digest-YYYY-MM-DD.md` is gitignored and written only
by the Archivist seat, so `requires_task_for_any_touched_file` exempts that one path
for that one agent type, the way it exempts a plan-writer's plan artifacts. Every
other agent type, and every other path the Archivist touches, stays gated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT = "/work/main"
DIGEST = f"{PROJECT}/.gobby/reports/gobby-digest-2026-10-28.md"


def _requires_task(
    tool_input: dict[str, object],
    agent_type: str | None,
    *,
    project_path: str = PROJECT,
) -> bool:
    """Call the gate as `require-task-before-edit` does for a native Write/Edit."""
    event_data = {
        "canonical_repo_mutation": True,
        "canonical_structured_mutation": True,
        "project_path": project_path,
    }
    return requires_task_for_any_touched_file(
        tool_input, "claude_code", False, event_data, agent_type
    )


@pytest.mark.parametrize("tool_input_key", ["file_path", "file_paths"])
def test_archivist_digest_write_needs_no_claim(tool_input_key: str) -> None:
    """A structured Write/Edit of the dated digest is exempt for the archivist seat."""
    target: object = DIGEST if tool_input_key == "file_path" else [DIGEST]

    assert _requires_task({tool_input_key: target}, "archivist") is False


@pytest.mark.parametrize(
    "agent_type",
    [None, "developer", "test-writer", "code-reviewer", "plan-writer", "assistant", "orchestrator"],
)
def test_other_agent_types_still_need_a_claim_for_the_digest(agent_type: str | None) -> None:
    """The exemption follows the agent type, not the path."""
    assert _requires_task({"file_path": DIGEST}, agent_type) is True


@pytest.mark.parametrize(
    "path",
    [
        f"{PROJECT}/src/gobby/example.py",
        f"{PROJECT}/.gobby/reports/notes.md",
        f"{PROJECT}/.gobby/reports/gobby-digest-latest.md",
        f"{PROJECT}/.gobby/reports/sub/gobby-digest-2026-10-28.md",
        f"{PROJECT}/.gobby/reports/gobby-digest-2026-10-28.mdx",
        # The staging sibling is optional and "remains subject to the normal task gate".
        f"{PROJECT}/.gobby/reports/gobby-digest-2026-10-28.md.tmp.123.0123456789ab",
        # A digest under another project's root is not this project's role-owned path.
        "/work/other/.gobby/reports/gobby-digest-2026-10-28.md",
        # Normalization must not let a traversal reach a gated file through the digest dir.
        f"{PROJECT}/.gobby/reports/../../src/gobby/example.py",
    ],
)
def test_archivist_outside_the_digest_still_needs_a_claim(path: str) -> None:
    """The exemption is the dated digest alone, not a blanket archivist bypass."""
    assert _requires_task({"file_path": path}, "archivist") is True


def test_archivist_touching_the_digest_and_another_file_needs_a_claim() -> None:
    """Every touched path must be exempt; one gated file gates the whole mutation."""
    tool_input: dict[str, object] = {"file_paths": [DIGEST, f"{PROJECT}/src/gobby/example.py"]}

    assert _requires_task(tool_input, "archivist") is True


async def _engine_decision(
    db: HubDatabase, checkout: Path, target: Path, agent_type: str, tool_name: str
) -> str:
    """Evaluate the bundled `require-task-before-edit` rule for an unclaimed native edit."""
    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    db.execute("UPDATE rule_definitions SET enabled = (name = 'require-task-before-edit')")
    data: dict[str, object] = {
        "tool_name": tool_name,
        "tool_input": {"file_path": str(target), "content": "digest\n"},
        "project_path": str(checkout),
    }
    normalize_tool_fields(data)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(checkout),
        data=data,
    )
    response = await RuleEngine(db).evaluate(
        event,
        session_id=SESSION_ID,
        variables={
            "require_task_before_edit": True,
            "task_claimed": False,
            "plan_mode": False,
            "_agent_type": agent_type,
        },
    )
    return response.decision


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
async def test_rule_lets_an_unclaimed_archivist_write_the_digest(
    temp_db: HubDatabase, tmp_path: Path, tool_name: str
) -> None:
    """The bundled rule, end to end: an unclaimed archivist is not blocked on the digest."""
    checkout = tmp_path / "main"
    (checkout / ".git").mkdir(parents=True)
    digest = checkout / ".gobby/reports/gobby-digest-2026-10-28.md"

    assert await _engine_decision(temp_db, checkout, digest, "archivist", tool_name) == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["developer", "test-writer"])
async def test_rule_still_blocks_other_unclaimed_seats_on_the_digest(
    temp_db: HubDatabase, tmp_path: Path, agent_type: str
) -> None:
    """The same edit stays blocked for every other agent type."""
    checkout = tmp_path / "main"
    (checkout / ".git").mkdir(parents=True)
    digest = checkout / ".gobby/reports/gobby-digest-2026-10-28.md"

    assert await _engine_decision(temp_db, checkout, digest, agent_type, "Edit") == "block"
