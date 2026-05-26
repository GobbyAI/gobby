"""Tests for reviewer terminal-verdict enforcement after successful validation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.condition_helpers import is_validation_command
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit


RULE_NAMES = {
    "reviewer-terminal-verdict-track-successful-validation",
    "reviewer-terminal-verdict-clear-on-verdict",
    "reviewer-terminal-verdict-block-turn-end",
}


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    """Create a rule-synced PostgreSQL fixture database."""
    database = temp_db
    result = sync_bundled_rules(database, get_bundled_rules_path())
    assert result["errors"] == []
    yield database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    """Return workflow definition storage for the fixture database."""
    return LocalWorkflowDefinitionManager(db)


def _event(
    event_type: HookEventType,
    data: dict[str, Any],
    *,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
    )


def _validation_event(command: str, *, success: bool = True) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_output": {"success": success},
    }
    if not success:
        data["is_error"] = True
    return _event(HookEventType.AFTER_TOOL, data)


def _cmd_validation_event(command: str) -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "exec_command",
            "tool_input": {"cmd": command},
            "tool_output": {"success": True},
        },
        source=SessionSource.CODEX,
    )


def _review_vars(agent_type: str) -> dict[str, Any]:
    return {
        "_agent_type": agent_type,
        "current_step": "review",
        "review_complete": False,
        "is_spawned_agent": True,
    }


def _verdict_event(server: str, tool: str) -> HookEvent:
    return _event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": server,
                "tool_name": tool,
                "arguments": {"task_id": "task-1", "stage_name": "development"},
            },
            "tool_output": {"success": True, "result": {"success": True}},
        },
    )


def _rule(manager: LocalWorkflowDefinitionManager, name: str) -> RuleDefinitionBody:
    row = manager.get_by_name(name)
    assert row is not None
    return RuleDefinitionBody.model_validate_json(row.definition_json)


@pytest.mark.parametrize(
    "command",
    [
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_qa.py -v",
        "cd repo && uv run ruff check src/gobby tests/workflows",
        "uv run ruff format --check src/gobby",
        "uv run --project . python -m pytest tests/foo.py",
        "uv run --cache-dir /tmp/uv-cache --color never --python 3.13 pytest tests/foo.py",
        "uv run -p 3.13 -C setup-args pytest tests/foo.py",
        "uv run --no-binary pytest",
        "uv run --no-dev pytest tests/foo.py",
        "python3 -m mypy src/gobby",
        "python -m coverage run -m pytest tests/foo.py",
        "tox -e py313",
        "nox -s tests",
        "vitest run",
        "jest --runInBand",
        "npm run test -- --watch=false",
        "pnpm lint",
        "bun test",
        "cargo test --workspace",
        "go test ./...",
        "make test",
    ],
)
def test_validation_command_detection_accepts_common_validation_commands(command: str) -> None:
    """Common validation commands are recognized across package managers."""
    assert is_validation_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "",
        "rg pytest src tests",
        "echo 'uv run pytest tests/foo.py'",
        "git status --short",
        "uv run python -c 'print(\"pytest\")'",
    ],
)
def test_validation_command_detection_rejects_non_validation_commands(command: str) -> None:
    """Non-validation shell commands do not satisfy the reviewer gate."""
    assert is_validation_command(command) is False


def test_bundled_reviewer_terminal_verdict_rules_sync(
    manager: LocalWorkflowDefinitionManager,
) -> None:
    """Bundled reviewer terminal-verdict rules sync with the expected contract."""
    rule_names = {row.name for row in manager.list_all(workflow_type="rule")}

    assert RULE_NAMES <= rule_names
    for name in RULE_NAMES:
        body = _rule(manager, name)
        assert body.group == "reviewer-lifecycle"
        assert body.agent_scope == ["qa-reviewer", "doc-reviewer", "holistic-reviewer"]
    track_body = _rule(manager, "reviewer-terminal-verdict-track-successful-validation")
    assert "is_validation_command" in (track_body.when or "")


@pytest.mark.asyncio
async def test_qa_reviewer_successful_validation_sets_pending_and_injects_reminder(
    db: HubDatabase,
) -> None:
    """QA reviewer validation success sets pending state and injects verdict guidance."""
    engine = RuleEngine(db)
    variables = _review_vars("qa-reviewer")
    event = _validation_event(
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_qa_reviewer_definition.py -q"
    )

    response = await engine.evaluate(event, "sess-qa", variables)

    assert response.decision == "allow"
    assert variables["reviewer_terminal_verdict_pending"] is True
    assert variables["reviewer_terminal_verdict_validation_command"] == (
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_qa_reviewer_definition.py -q"
    )
    assert "Reviewer validation succeeded" in (response.context or "")
    assert 'approve_review(stage_name="development")' in (response.context or "")


@pytest.mark.asyncio
async def test_codex_cmd_alias_validation_sets_pending(db: HubDatabase) -> None:
    """Codex exec_command validation aliases are accepted as validation commands."""
    engine = RuleEngine(db)
    variables = _review_vars("qa-reviewer")
    event = _cmd_validation_event(
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_qa_reviewer_definition.py -q"
    )

    response = await engine.evaluate(event, "sess-codex", variables)

    assert response.decision == "allow"
    assert variables["reviewer_terminal_verdict_pending"] is True
    assert variables["reviewer_terminal_verdict_validation_command"] == (
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_qa_reviewer_definition.py -q"
    )


@pytest.mark.asyncio
async def test_python_module_ruff_format_check_is_validation_command(
    db: HubDatabase,
) -> None:
    """`python -m ruff format --check` counts as validation, including through uv."""
    engine = RuleEngine(db)
    variables = _review_vars("qa-reviewer")
    event = _cmd_validation_event("uv run python -m ruff format --check src/gobby")

    response = await engine.evaluate(event, "sess-codex", variables)

    assert response.decision == "allow"
    assert variables["reviewer_terminal_verdict_pending"] is True


@pytest.mark.asyncio
async def test_doc_reviewer_successful_validation_uses_review_verdict_contract(
    db: HubDatabase,
) -> None:
    """Doc reviewer validation success requests the review verdict tools."""
    engine = RuleEngine(db)
    variables = _review_vars("doc-reviewer")
    event = _validation_event("uv run ruff check src/gobby/install/shared/workflows/agents")

    response = await engine.evaluate(event, "sess-doc", variables)

    assert response.decision == "allow"
    assert variables["reviewer_terminal_verdict_pending"] is True
    assert 'reject_review(stage_name="development")' in (response.context or "")
    assert "complete_stage" not in (response.context or "")


@pytest.mark.asyncio
async def test_holistic_reviewer_successful_validation_requires_stage_verdict(
    db: HubDatabase,
) -> None:
    """Holistic reviewer validation success requests holistic QA stage verdict tools."""
    engine = RuleEngine(db)
    variables = _review_vars("holistic-reviewer")
    event = _validation_event("uv run mypy src/gobby/workflows")

    response = await engine.evaluate(event, "sess-holistic", variables)

    assert response.decision == "allow"
    assert variables["reviewer_terminal_verdict_pending"] is True
    assert 'complete_stage(stage_name="holistic_qa")' in (response.context or "")
    assert 'fail_stage(stage_name="holistic_qa")' in (response.context or "")


@pytest.mark.asyncio
async def test_failed_validation_does_not_set_pending(db: HubDatabase) -> None:
    """Failed validation does not mark the terminal verdict as pending."""
    engine = RuleEngine(db)
    variables = _review_vars("qa-reviewer")
    event = _validation_event(
        "GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_qa_reviewer_definition.py -q",
        success=False,
    )

    response = await engine.evaluate(event, "sess-failed", variables)

    assert response.decision == "allow"
    assert variables.get("reviewer_terminal_verdict_pending") is None


@pytest.mark.asyncio
async def test_terminal_verdict_success_clears_pending(db: HubDatabase) -> None:
    """QA reviewer approval clears pending terminal-verdict variables."""
    engine = RuleEngine(db)
    variables = {
        **_review_vars("qa-reviewer"),
        "reviewer_terminal_verdict_pending": True,
        "reviewer_terminal_verdict_validation_command": "uv run pytest tests/agents -q",
    }
    event = _verdict_event("gobby-tasks-ops", "approve_review")

    response = await engine.evaluate(event, "sess-clear", variables)

    assert response.decision == "allow"
    assert variables["reviewer_terminal_verdict_pending"] is False
    assert variables["reviewer_terminal_verdict_validation_command"] == ""


@pytest.mark.asyncio
async def test_holistic_terminal_verdict_success_clears_pending(db: HubDatabase) -> None:
    """Holistic reviewer completion clears pending terminal-verdict variables."""
    engine = RuleEngine(db)
    variables = {
        **_review_vars("holistic-reviewer"),
        "reviewer_terminal_verdict_pending": True,
        "reviewer_terminal_verdict_validation_command": "uv run mypy src/gobby/workflows",
    }
    event = _verdict_event("gobby-tasks-ops", "complete_stage")

    response = await engine.evaluate(event, "sess-holistic-clear", variables)

    assert response.decision == "allow"
    assert variables["reviewer_terminal_verdict_pending"] is False


@pytest.mark.asyncio
async def test_turn_end_blocks_while_terminal_verdict_pending(db: HubDatabase) -> None:
    """Turn end is blocked until a reviewer submits the terminal verdict."""
    engine = RuleEngine(db)
    variables = {
        **_review_vars("doc-reviewer"),
        "reviewer_terminal_verdict_pending": True,
        "reviewer_terminal_verdict_validation_command": "uv run ruff check docs",
        "stop_attempts": 0,
    }
    event = _event(HookEventType.AFTER_AGENT, {})

    response = await engine.evaluate(event, "sess-block", variables)

    assert response.decision == "block"
    assert "reviewer-terminal-verdict-block-turn-end" in (response.reason or "")
    assert 'approve_review(stage_name="development")' in (response.reason or "")
    assert variables["stop_attempts"] == 1
