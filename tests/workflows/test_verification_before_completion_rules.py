"""Tests for verification-before-completion lifecycle rule wiring."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


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


def _rule(manager: LocalWorkflowDefinitionManager, name: str) -> RuleDefinitionBody:
    row = manager.get_by_name(name)
    assert row is not None
    return RuleDefinitionBody.model_validate_json(row.definition_json)


def test_schema_lookup_rule_mentions_lifecycle_completion_tools(db, manager) -> None:
    _sync_bundled(db)

    body = _rule(manager, "require-verification-before-completion-on-schema")

    assert body.event.value == "before_tool"
    assert body.effects[0].type == "block"
    assert body.effects[0].reason == skill_fetch_directive("verification-before-completion")
    assert "not skill_loaded('verification-before-completion')" in (body.when or "")
    when = body.when or ""
    for tool_name in (
        "close_task",
        "submit_for_review",
        "approve_review",
        "record_pr_opened",
        "open_delivery_pr",
        "record_pr_verdict",
        "record_merge_result",
        "close_linked_github_issue",
        "merge_apply",
    ):
        assert tool_name in when


def test_lifecycle_call_rule_requires_verification_skill(db, manager) -> None:
    _sync_bundled(db)

    body = _rule(manager, "require-verification-before-completion-on-lifecycle-call")
    block_effects = [effect for effect in body.effects if effect.type == "block"]

    assert body.event.value == "before_tool"
    assert "not skill_loaded('verification-before-completion')" in (body.when or "")
    assert len(block_effects) == 1
    assert block_effects[0].reason == skill_fetch_directive("verification-before-completion")


def _close_task_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby-tasks__close_task",
            "mcp_server": "gobby-tasks",
            "mcp_tool": "close_task",
            "tool_input": {
                "task_id": "#42",
                "commit_sha": "abc1234",
                "changes_summary": "done",
            },
        },
    )


def _set_variable_event(name: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": {"name": name, "value": True, "session_id": "#1"},
        },
    )


def _ready_variables(**overrides: object) -> dict[str, object]:
    variables: dict[str, object] = {
        "loaded_skills": ["task-transitions", "verification-before-completion"],
        "session_edited_files": ["src/gobby/workflows/observers.py"],
        "task_has_commits": True,
        "errors_resolved": True,
        "memory_review_completed": True,
        "verification_evidence_recorded": True,
    }
    variables.update(overrides)
    return variables


@pytest.mark.asyncio
async def test_completion_readiness_blocks_without_recorded_evidence(db, manager) -> None:
    """Lifecycle success tools are blocked until fresh verification evidence exists."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _close_task_event(),
        session_id="sid",
        variables=_ready_variables(verification_evidence_recorded=False),
    )

    assert response.decision == "block"
    assert "require-completion-readiness-evidence" in (response.reason or "")


@pytest.mark.asyncio
async def test_completion_readiness_allows_with_all_sibling_gates(db, manager) -> None:
    """Completion readiness allows close_task when every sibling gate is satisfied."""
    _sync_bundled(db)

    response = await RuleEngine(db).evaluate(
        _close_task_event(),
        session_id="sid",
        variables=_ready_variables(),
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_protected_evidence_variables_cannot_be_set_directly(db, manager) -> None:
    """Agents must record evidence through approved observers or MCP tools."""
    _sync_bundled(db)

    for name in ("verification_evidence_recorded", "verification_evidence"):
        response = await RuleEngine(db).evaluate(
            _set_variable_event(name),
            session_id="sid",
            variables={},
        )

        assert response.decision == "block"
        assert "block-direct-verification-evidence-variable-set" in (response.reason or "")
