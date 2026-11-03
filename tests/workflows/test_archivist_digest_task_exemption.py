"""A seat granted the digest capability edits its dated digest without a claim.

`require-task-before-edit` gates repository writes on a claimed task. The digest at
`<project>/.gobby/reports/gobby-digest-YYYY-MM-DD.md` is gitignored and is a taskless
role duty. A definition opts its seat in by declaring the `write_digest_without_task`
capability; the rule passes that grant to `requires_task_for_any_touched_file` as
`allow_digest`, the same way `write_plan_artifacts_without_task` reaches
`allow_plan_artifacts`. The gate never branches on an agent-definition name (AGENTS.md
Working Rule 16), so every engine case runs under two definition names. Seats without
the grant, and every other path, stay gated.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

PROJECT = "/work/main"
DIGEST = f"{PROJECT}/.gobby/reports/gobby-digest-2026-10-28.md"
DIGEST_RELATIVE = ".gobby/reports/gobby-digest-2026-10-28.md"
CAPABILITY = "write_digest_without_task"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


def _requires_task(
    tool_input: dict[str, object],
    *,
    allow_digest: bool,
    allow_plan_artifacts: bool = False,
) -> bool:
    """Call the gate as `require-task-before-edit` does for a native Write/Edit."""
    event_data = {
        "canonical_repo_mutation": True,
        "canonical_structured_mutation": True,
        "project_path": PROJECT,
    }
    return requires_task_for_any_touched_file(
        tool_input,
        "claude_code",
        False,
        event_data,
        allow_plan_artifacts,
        allow_digest=allow_digest,
    )


@pytest.mark.parametrize("tool_input_key", ["file_path", "file_paths"])
def test_granted_digest_write_needs_no_claim(tool_input_key: str) -> None:
    """A structured Write/Edit of the dated digest is exempt under the grant."""
    target: object = DIGEST if tool_input_key == "file_path" else [DIGEST]

    assert _requires_task({tool_input_key: target}, allow_digest=True) is False


@pytest.mark.parametrize("allow_plan_artifacts", [False, True])
def test_ungranted_digest_write_still_needs_a_claim(allow_plan_artifacts: bool) -> None:
    """Without the digest grant the digest stays gated, even under the plan-artifact grant."""
    assert (
        _requires_task(
            {"file_path": DIGEST}, allow_digest=False, allow_plan_artifacts=allow_plan_artifacts
        )
        is True
    )


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
def test_grant_outside_the_digest_still_needs_a_claim(path: str) -> None:
    """The grant covers the dated digest alone, not a blanket bypass."""
    assert _requires_task({"file_path": path}, allow_digest=True) is True


def test_digest_and_another_file_needs_a_claim() -> None:
    """Every touched path must be exempt; one gated file gates the whole mutation."""
    tool_input: dict[str, object] = {"file_paths": [DIGEST, f"{PROJECT}/src/gobby/example.py"]}

    assert _requires_task(tool_input, allow_digest=True) is True


@pytest.fixture
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _seat_definition(
    db: HubDatabase, session: Session, name: str, *, granted: bool, enabled: bool
) -> None:
    """A synthetic seat definition; only its capability list matters to the gate."""
    AgentDefinitionManager(db).create(
        name,
        {
            "name": name,
            "prompts": {"agent": "Write the dated digest."},
            "workflows": {"rule_selectors": {"include": ["name:require-task-before-edit"]}},
            "capabilities": [CAPABILITY] if granted else [],
        },
        project_id=session.project_id,
        enabled=enabled,
    )
    SessionVariableManager(db).set_variable(session.id, "_agent_type", name)


def _edit_event(session: Session, target: Path, checkout: Path, tool_name: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session.external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(checkout),
        project_id=session.project_id,
        data={
            "tool_name": tool_name,
            "tool_input": {"file_path": str(target), "content": "digest\n"},
            "canonical_repo_mutation": True,
            "canonical_structured_mutation": True,
            "canonical_file_paths": [str(target)],
            "project_path": str(checkout),
        },
        metadata={"_platform_session_id": session.id, "project_path": str(checkout)},
    )
