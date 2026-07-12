"""Tests for completion-readiness lifecycle rule wiring."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    database = temp_db
    yield database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    # Test-only bypass: source-change validation is the behavior under test,
    # and the official update API would only add unrelated workflow policy checks.
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")


def _lifecycle_event(
    server: str = "gobby-tasks",
    tool: str = "close_task",
    task_id: str = "#42",
) -> HookEvent:
    arguments = {
        "task_id": task_id,
        "commit_sha": "abc1234",
        "changes_summary": "done",
    }
    if server == "gobby-tasks-ops":
        arguments["stage_name"] = "development"
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": server,
            "mcp_tool": tool,
            "tool_input": {
                "server_name": server,
                "tool_name": tool,
                "arguments": arguments,
            },
        },
    )


def _set_variable_event(name: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": {"name": name, "value": True, "session_id": "#1"},
        },
    )


def _ready_variables(**overrides: object) -> dict[str, object]:
    variables: dict[str, object] = {
        "loaded_skills": ["task-transitions"],
        "target_task_has_edits": True,
        "task_has_commits": True,
        "memory_review_completed": True,
        "is_spawned_agent": True,
        "verification_evidence": [
            {
                "evidence_type": "validation_command",
                "command": "uv run pytest tests/workflows/test_hooks.py -v",
                "success": True,
            }
        ],
        "verification_evidence_recorded": True,
    }
    variables.update(overrides)
    return variables


def test_completion_readiness_when_uses_target_task_edits(
    db: HubDatabase,
    manager: LocalWorkflowDefinitionManager,
) -> None:
    """Completion readiness should not use session-wide edit state."""
    _sync_bundled(db)

    row = manager.get_by_name("require-completion-readiness-evidence")
    assert row is not None
    body = RuleDefinitionBody.model_validate_json(row.definition_json)

    assert body.when is not None
    assert "target_task_has_edits" in body.when
    assert "session_edited_files" not in body.when


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "tool"),
    [
        ("gobby-tasks", "close_task"),
        ("gobby-tasks-ops", "submit_for_review"),
    ],
)
async def test_completion_readiness_blocks_without_evidence(
    db: HubDatabase,
    server: str,
    tool: str,
) -> None:
    """Lifecycle success tools are blocked until fresh verification evidence exists."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _lifecycle_event(server, tool),
        session_id=SESSION_ID,
        variables=_ready_variables(verification_evidence=[], verification_evidence_recorded=False),
    )

    assert response.decision == "block"
    assert "require-completion-readiness-evidence" in (response.reason or "")


@pytest.mark.asyncio
async def test_completion_readiness_allows_successful_validation_evidence(
    db: HubDatabase,
) -> None:
    """A successful validation command satisfies completion readiness."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _lifecycle_event(),
        session_id=SESSION_ID,
        variables=_ready_variables(),
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_completion_readiness_isolates_concurrent_task_evidence(
    db: HubDatabase,
) -> None:
    """A later failure for task B cannot invalidate task A's scoped success."""
    _sync_bundled(db)
    variables = _ready_variables(
        verification_evidence=[
            {
                "evidence_type": "test",
                "task_id": "#42",
                "supports": "task_id:#42",
                "success": True,
            },
            {
                "evidence_type": "validation_command",
                "task_id": "#99",
                "success": False,
            },
        ]
    )

    task_a_response = await RuleEngine(db).evaluate(
        _lifecycle_event(task_id="#42"),
        session_id=SESSION_ID,
        variables=variables,
    )
    task_b_response = await RuleEngine(db).evaluate(
        _lifecycle_event(task_id="#99"),
        session_id=SESSION_ID,
        variables=variables,
    )

    assert task_a_response.decision == "allow"
    assert task_b_response.decision == "block"
    assert "require-completion-readiness-evidence" in (task_b_response.reason or "")


@pytest.mark.asyncio
async def test_completion_readiness_blocks_failed_validation_evidence(
    db: HubDatabase,
) -> None:
    """The latest failed validation command blocks lifecycle success."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _lifecycle_event(),
        session_id=SESSION_ID,
        variables=_ready_variables(
            verification_evidence=[
                {
                    "evidence_type": "validation_command",
                    "command": "uv run pytest tests/workflows/test_hooks.py -v",
                    "success": False,
                }
            ],
            verification_evidence_recorded=False,
        ),
    )

    assert response.decision == "block"
    assert "require-completion-readiness-evidence" in (response.reason or "")


@pytest.mark.asyncio
async def test_later_successful_validation_clears_failed_validation_block(
    db: HubDatabase,
) -> None:
    """A later validation-command success resolves an earlier validation failure."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _lifecycle_event(),
        session_id=SESSION_ID,
        variables=_ready_variables(
            verification_evidence=[
                {
                    "evidence_type": "validation_command",
                    "command": "uv run pytest old.py",
                    "success": False,
                },
                {
                    "evidence_type": "validation_command",
                    "command": "uv run pytest new.py",
                    "success": True,
                },
            ],
        ),
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_manual_evidence_satisfies_readiness_without_failed_validation(
    db: HubDatabase,
) -> None:
    """Manual evidence is sufficient when no failed validation command is pending."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _lifecycle_event(),
        session_id=SESSION_ID,
        variables=_ready_variables(
            verification_evidence=[
                {
                    "evidence_type": "manual_diff_review",
                    "summary": "Reviewed diff",
                    "success": True,
                }
            ],
        ),
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_manual_evidence_cannot_clear_failed_validation(
    db: HubDatabase,
) -> None:
    """Manual evidence does not resolve a failed validation command."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _lifecycle_event(),
        session_id=SESSION_ID,
        variables=_ready_variables(
            verification_evidence=[
                {
                    "evidence_type": "validation_command",
                    "command": "uv run pytest old.py",
                    "success": False,
                },
                {
                    "evidence_type": "manual_diff_review",
                    "summary": "Reviewed diff",
                    "success": True,
                },
            ],
        ),
    )

    assert response.decision == "block"
    assert "require-completion-readiness-evidence" in (response.reason or "")


@pytest.mark.asyncio
async def test_protected_evidence_variables_cannot_be_set_directly(
    db: HubDatabase,
) -> None:
    """Agents must record evidence through approved observers or MCP tools."""
    _sync_bundled(db)

    for name in ("verification_evidence_recorded", "verification_evidence"):
        response = await RuleEngine(db).evaluate(
            _set_variable_event(name),
            session_id=SESSION_ID,
            variables={},
        )

        assert response.decision == "block"
        assert "block-direct-verification-evidence-variable-set" in (response.reason or "")
