"""Tests for stop-gates rules and hardcoded engine plumbing.

Tier 1 behaviors (hardcoded in RuleEngine.evaluate):
- stop_attempts auto-increment on STOP
- BEFORE_AGENT full reset (tool_block_pending, errors_resolved, stop_attempts, etc.)
- tool_block_pending stop gate, force_allow_stop bypass, consecutive tool block counter

Tier 2 rules (YAML templates — configurable):
- require-error-triage-before-status (task-enforcement), require-task-close
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.tasks.state_semantics import ACTIVE_STAGE_STATES
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_stop_gates.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    return sync_bundled_rules(db, get_bundled_rules_path())


def _get_rule(manager, name):
    """Get a bundled rule by name."""
    return manager.get_by_name(name)


STOP_GATES_RULES = {
    "require-task-close",
}


class TestStopGatesSync:
    """Test that stop-gates rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All stop-gates rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert STOP_GATES_RULES.issubset(rule_names), f"Missing: {STOP_GATES_RULES - rule_names}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All rules should have group='stop-gates'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in STOP_GATES_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "stop-gates", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in STOP_GATES_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                for effect in body.resolved_effects:
                    assert effect.type in {"block", "set_variable"}


class TestStopAttemptsPlumbing:
    """Test hardcoded stop_attempts increment in RuleEngine.

    stop_attempts increments on every STOP event before any gate checks.
    It resets on BEFORE_AGENT (new user turn).
    """

    @pytest.mark.asyncio
    async def test_stop_increments_stop_attempts(self, db) -> None:
        """STOP event should auto-increment stop_attempts."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("stop_attempts") == 1

    @pytest.mark.asyncio
    async def test_stop_increments_from_existing_value(self, db) -> None:
        """stop_attempts should increment from current value."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"stop_attempts": 3}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("stop_attempts") == 4

    @pytest.mark.asyncio
    async def test_turn_start_boundary_resets_stop_attempts(self, db) -> None:
        """The semantic turn_start boundary should reset stop_attempts to 0."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"stop_attempts": 5}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("stop_attempts") == 0

    @pytest.mark.asyncio
    async def test_stop_attempts_increments_even_when_force_allowed(self, db) -> None:
        """stop_attempts should increment even when stop is force-allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "stop_attempts": 2,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "allow"
        assert variables.get("stop_attempts") == 3

    @pytest.mark.asyncio
    async def test_stop_attempts_increments_even_when_blocked(self, db) -> None:
        """stop_attempts should increment even when stop is blocked by tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "stop_attempts": 1,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "block"
        assert variables.get("stop_attempts") == 2


class TestRequireErrorTriage:
    """Verify require-error-triage-before-status blocks status transitions until triage confirmed."""

    def test_blocks_on_before_tool(self, db, manager) -> None:
        """Should have a block effect on before_tool event."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-error-triage-before-status")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        effect_types = {e.type for e in body.resolved_effects}
        assert "block" in effect_types

    def test_blocks_all_status_transitions(self, db, manager) -> None:
        """Should block close_task and all review lifecycle transitions."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-error-triage-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        mcp_tools = body.effects[0].mcp_tools
        assert "gobby-tasks:close_task" in mcp_tools
        assert "gobby-tasks:de_escalate_task" in mcp_tools
        assert "gobby-tasks-ops:submit_for_review" in mcp_tools
        assert "gobby-tasks-ops:approve_review" in mcp_tools
        assert "gobby-tasks-ops:reject_review" in mcp_tools

    def test_when_checks_triage_flag(self, db, manager) -> None:
        """Should check errors_resolved."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-error-triage-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "errors_resolved" in body.when
        assert "session_edited_files" in body.when


class TestRequireTaskClose:
    """Verify require-task-close blocks stop if task in_progress."""

    def test_blocks_on_stop(self, db, manager) -> None:
        """Should be a block effect on semantic turn_end."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-task-close")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_end"
        assert body.effects[0].type == "block"

    def test_when_checks_mode_level_and_task(self, db, manager) -> None:
        """Should check mode_level and task_claimed."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-task-close")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "mode_level" in body.when
        assert "task_claimed" in body.when

    def test_does_not_block_when_task_claimed_unset(self, db, manager) -> None:
        """Should NOT block when task_claimed was never set (no false positive)."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-task-close")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        variables: dict[str, object] = {"mode_level": 2, "stop_attempts": 1}
        evaluator = SafeExpressionEvaluator(
            context={"variables": variables},
            allowed_funcs={"len": len, "str": str, "int": int, "bool": bool},
        )
        assert not evaluator.evaluate(body.when), "Rule should not fire when task_claimed is unset"

    def test_blocks_when_task_claimed_is_set(self, db, manager) -> None:
        """Should block when task_claimed is set and conditions met."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-task-close")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        variables: dict[str, object] = {
            "mode_level": 2,
            "stop_attempts": 1,
            "task_claimed": True,
            "claimed_tasks": {"task-123": "#1"},
        }
        evaluator = SafeExpressionEvaluator(
            context={"variables": variables},
            allowed_funcs={"len": len, "str": str, "int": int, "bool": bool},
        )
        assert evaluator.evaluate(body.when), "Rule should fire when task_claimed is set"

    @pytest.mark.asyncio
    async def test_blocks_on_after_agent_turn_end(self, db, manager) -> None:
        """Bundled turn_end gate should also fire for Gemini/Codex after-agent hooks."""
        _sync_bundled(db)

        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "mode_level": 2,
            "task_claimed": True,
            "claimed_tasks": {"task-123": "#1"},
            "stop_attempts": 0,
        }

        event = _make_event(
            HookEventType.AFTER_AGENT,
            source=SessionSource.GEMINI,
        )
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "block"
        assert "require-task-close" in (response.reason or "")


class TestCompactPreservesTriagedState:
    """Regression: compact must NOT reset errors_resolved.

    Bug scenario: agent sets errors_resolved=true during session,
    then /compact fires SessionStart → _activate_default_agent re-applies
    defaults → overwrites triaged back to false → require-error-triage fires
    spuriously on next stop.
    """

    def test_compact_preserves_triaged_state(self, db, manager) -> None:
        """After triaging errors, compact should NOT cause require-error-triage to fire."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-error-triage-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        # State AFTER agent has triaged errors and committed
        variables: dict[str, object] = {
            "errors_resolved": True,  # Set by agent
            "stop_attempts": 1,
        }

        # The fix ensures _activate_default_agent does NOT overwrite these.
        # Verify: with preserved variables, the rule condition should NOT match.
        # Build event context that would normally trigger the rule (close_task with commit)
        mock_event = MagicMock()
        mock_event.data = {
            "mcp_tool": "close_task",
            "tool_input": {"arguments": {"commit_sha": "abc123"}},
        }
        evaluator = SafeExpressionEvaluator(
            context={"variables": variables, "event": mock_event},
            allowed_funcs={"len": len, "str": str, "int": int, "bool": bool},
        )
        assert not evaluator.evaluate(body.when), (
            "require-error-triage-before-status should NOT fire when errors_resolved=true"
        )


class TestBeforeAgentResetsPlumbing:
    """Test hardcoded BEFORE_AGENT resets in RuleEngine.

    The semantic turn_start boundary clears per-turn stop-cycle state: tool_block_pending,
    stop_attempts, consecutive_tool_blocks, _last_blocked_tool.
    It does NOT reset errors_resolved (session-scoped).
    """

    @pytest.mark.asyncio
    async def test_clears_tool_block_pending(self, db) -> None:
        """The semantic turn_start boundary should clear tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_preserves_errors_resolved(self, db) -> None:
        """The semantic turn_start boundary should NOT reset errors_resolved."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"errors_resolved": True}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("errors_resolved") is True

    @pytest.mark.asyncio
    async def test_full_reset_on_new_turn(self, db) -> None:
        """The semantic turn_start boundary should reset stop-cycle variables."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "errors_resolved": True,
            "stop_attempts": 5,
            "consecutive_tool_blocks": 2,
            "_last_blocked_tool": "Edit",
        }

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables["tool_block_pending"] is False
        assert variables["errors_resolved"] is True
        assert variables["stop_attempts"] == 0
        assert variables["consecutive_tool_blocks"] == 0
        assert variables["_last_blocked_tool"] == ""


def _make_event(
    event_type: HookEventType,
    data: dict | None = None,
    metadata: dict | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    """Helper to create HookEvent for rule engine tests."""
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=source,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


class TestToolBlockPendingPlumbing:
    """Test hardcoded plumbing in RuleEngine for tool-error stop blocking.

    These behaviors are baked into evaluate() — no installed rules required.
    """

    @pytest.mark.asyncio
    async def test_after_tool_failure_sets_tool_block_pending(self, db) -> None:
        """Failed after_tool should auto-set tool_block_pending=True."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Edit", "is_error": True},
        )
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("tool_block_pending") is True

    @pytest.mark.asyncio
    async def test_after_tool_failure_via_metadata(self, db) -> None:
        """Failed after_tool via metadata.is_failure should also set flag."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Edit"},
            metadata={"is_failure": True},
        )
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("tool_block_pending") is True

    @pytest.mark.asyncio
    async def test_successful_after_tool_clears_tool_block_pending(self, db) -> None:
        """Successful after_tool should clear tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_stop_blocked_when_tool_block_pending(self, db) -> None:
        """Stop should be blocked when tool_block_pending is true."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "block"
        assert "tool just failed" in response.reason.lower()

    @pytest.mark.asyncio
    async def test_stop_block_is_self_clearing(self, db) -> None:
        """After blocking stop, tool_block_pending should be cleared."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_stop_allowed_without_tool_block_pending(self, db) -> None:
        """Stop should be allowed when tool_block_pending is not set."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_full_cycle_failure_then_stop_then_recovery(self, db) -> None:
        """End-to-end: tool fails → stop blocked → tool succeeds → stop allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. Tool fails
        fail_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Edit", "is_error": True},
        )
        await engine.evaluate(fail_event, "sess-1", variables)
        assert variables.get("tool_block_pending") is True

        # 2. Stop is blocked (and self-clears)
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, "sess-1", variables)
        assert response.decision == "block"
        assert variables.get("tool_block_pending") is False

        # 3. Tool succeeds
        ok_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )
        await engine.evaluate(ok_event, "sess-1", variables)

        # 4. Stop is allowed
        response = await engine.evaluate(stop_event, "sess-1", variables)
        assert response.decision == "allow"


class TestBashErrorStopGate:
    """End-to-end: Bash error detected via is_error → tool_block_pending → stop blocked.

    This tests the full flow that was broken before normalization Phase 3:
    Claude Code sends post-tool-use (not post-tool-use-failure) for Bash errors,
    so is_error must be inferred from tool output content.
    """

    @pytest.mark.asyncio
    async def test_bash_error_via_is_error_blocks_stop(self, db) -> None:
        """AFTER_TOOL with Bash is_error=True → tool_block_pending → STOP blocked."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. Bash tool fails (is_error set by normalization Phase 3)
        fail_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash", "is_error": True, "tool_output": "Exit code: 1"},
        )
        await engine.evaluate(fail_event, "sess-1", variables)
        assert variables.get("tool_block_pending") is True

        # 2. Stop is blocked
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, "sess-1", variables)
        assert response.decision == "block"
        assert "tool just failed" in response.reason.lower()

    @pytest.mark.asyncio
    async def test_bash_success_does_not_block_stop(self, db) -> None:
        """AFTER_TOOL with Bash success → no tool_block_pending → stop allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        ok_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash"},
        )
        await engine.evaluate(ok_event, "sess-1", variables)

        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, "sess-1", variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_full_bash_failure_recovery_cycle(self, db) -> None:
        """End-to-end: Bash fails → stop blocked → Bash succeeds → stop allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. Bash fails
        fail_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash", "is_error": True},
        )
        await engine.evaluate(fail_event, "sess-1", variables)
        assert variables.get("tool_block_pending") is True

        # 2. Stop blocked (self-clears)
        stop_event = _make_event(HookEventType.STOP)
        r1 = await engine.evaluate(stop_event, "sess-1", variables)
        assert r1.decision == "block"

        # 3. Bash succeeds
        ok_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash"},
        )
        await engine.evaluate(ok_event, "sess-1", variables)

        # 4. Stop allowed
        r2 = await engine.evaluate(stop_event, "sess-1", variables)
        assert r2.decision == "allow"


class TestForceAllowStop:
    """Test force_allow_stop catastrophic failure bypass.

    force_allow_stop bypasses all stop gates (including tool_block_pending)
    and is self-clearing after one use.
    """

    @pytest.mark.asyncio
    async def test_force_allow_stop_bypasses_stop_gates(self, db) -> None:
        """force_allow_stop should allow stop even with tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "tool_block_pending": True,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_force_allow_stop_is_self_clearing(self, db) -> None:
        """force_allow_stop should be cleared after use."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"force_allow_stop": True}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("force_allow_stop") is False

    @pytest.mark.asyncio
    async def test_force_allow_stop_only_works_once(self, db) -> None:
        """Second stop after force_allow_stop should use normal logic."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "tool_block_pending": True,
        }

        event = _make_event(HookEventType.STOP)

        # First stop — force allowed
        response = await engine.evaluate(event, "sess-1", variables)
        assert response.decision == "allow"

        # Re-set tool_block_pending (force_allow_stop is cleared now)
        variables["tool_block_pending"] = True

        # Second stop — normal blocking applies
        response = await engine.evaluate(event, "sess-1", variables)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_catastrophic_failure_sets_force_allow_stop(self, db) -> None:
        """Tool failure with catastrophic pattern should set force_allow_stop."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Bash",
                "is_error": True,
                "tool_output": "Error: You are out of usage for this billing period.",
            },
        )
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("force_allow_stop") is True

    @pytest.mark.asyncio
    async def test_catastrophic_rate_limit_detected(self, db) -> None:
        """Rate limit errors should trigger catastrophic bypass."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Bash",
                "is_error": True,
                "tool_output": "429 Too Many Requests: rate limit exceeded",
            },
        )
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("force_allow_stop") is True

    @pytest.mark.asyncio
    async def test_normal_failure_does_not_set_force_allow_stop(self, db) -> None:
        """Normal tool failure should NOT set force_allow_stop."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Edit",
                "is_error": True,
                "tool_output": "Error: old_string not found in file",
            },
        )
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("force_allow_stop") is not True

    @pytest.mark.asyncio
    async def test_catastrophic_then_stop_allowed(self, db) -> None:
        """End-to-end: catastrophic failure → stop is force-allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. Catastrophic failure
        fail_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Bash",
                "is_error": True,
                "tool_output": "quota exceeded — upgrade your plan",
            },
        )
        await engine.evaluate(fail_event, "sess-1", variables)
        assert variables.get("force_allow_stop") is True

        # 2. Stop is allowed (bypasses tool_block_pending too)
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, "sess-1", variables)
        assert response.decision == "allow"
        assert variables.get("force_allow_stop") is False


class TestConsecutiveBlockScoping:
    """Test that consecutive tool block counter is scoped to the same tool.

    The death spiral fix: when Tool A is blocked, only retries of Tool A
    escalate the counter. Attempting a different Tool B resets the counter
    and proceeds to normal rule evaluation, allowing the agent to recover.
    """

    @pytest.mark.asyncio
    async def test_same_tool_retried_5x_escalates(self, db) -> None:
        """Same tool retried 5 times should hit the configured escalation block."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "max_consecutive_blocked_tool_attempts": 5,
        }

        # Attempt 1: counter goes to 1, no escalation yet
        event1 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response1 = await engine.evaluate(event1, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 1
        # Not escalated yet — passes through to rule evaluation
        # (no rules installed, so it allows)
        assert response1.decision == "allow"

        # Re-set blocked-tool tracking (simulates the rule blocking it again)
        variables["_last_blocked_tool"] = "TodoWrite"

        # Attempt 2: counter goes to 2, still below threshold
        event2 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response2 = await engine.evaluate(event2, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 2
        assert response2.decision == "allow"

        variables["_last_blocked_tool"] = "TodoWrite"

        # Attempt 3: counter goes to 3, still below threshold
        event3 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response3 = await engine.evaluate(event3, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 3
        assert response3.decision == "allow"

        variables["_last_blocked_tool"] = "TodoWrite"

        # Attempt 4: counter goes to 4 → escalation block on the 5th total attempt
        event4 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response4 = await engine.evaluate(event4, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 4
        assert response4.decision == "block"
        assert "TodoWrite" in response4.reason
        assert "5 times" in response4.reason

    @pytest.mark.asyncio
    async def test_different_tool_resets_counter(self, db) -> None:
        """Different tool after a block should reset counter and proceed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "consecutive_tool_blocks": 1,
        }

        # Try a different tool (Read) — counter should reset
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
        )
        response = await engine.evaluate(event, "sess-1", variables)

        assert variables.get("consecutive_tool_blocks") == 0
        # No rules installed, so it allows through to normal evaluation
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_pipeline_direct_mcp_retry_clears_stale_counter(self, db) -> None:
        """Pipeline-scoped direct MCP calls should not inherit interactive retry state."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "gobby-workflows:list_pipeline_executions",
            "consecutive_tool_blocks": 2,
        }

        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-workflows",
                    "tool_name": "list_pipeline_executions",
                },
            },
            source=SessionSource.PIPELINE,
        )
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "allow"
        assert variables.get("consecutive_tool_blocks") == 0
        assert variables.get("_last_blocked_tool") == ""

    @pytest.mark.asyncio
    async def test_different_tool_blocked_starts_own_counter(self, db) -> None:
        """If a different tool is also rule-blocked, it starts its own counter."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "consecutive_tool_blocks": 1,
        }

        # Edit is a different tool — counter resets to 0
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )
        await engine.evaluate(event, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 0

        # Simulate Edit being blocked by a rule (updates last_blocked)
        variables["_last_blocked_tool"] = "Edit"

        # Retry Edit — counter goes to 1
        event2 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )
        await engine.evaluate(event2, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 1

    @pytest.mark.asyncio
    async def test_turn_start_boundary_resets_last_blocked_tool(self, db) -> None:
        """The semantic turn_start boundary should reset _last_blocked_tool."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "consecutive_tool_blocks": 2,
        }

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("_last_blocked_tool") == ""
        assert variables.get("consecutive_tool_blocks") == 0

    @pytest.mark.asyncio
    async def test_successful_after_tool_clears_last_blocked(self, db) -> None:
        """Successful AFTER_TOOL should clear _last_blocked_tool."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "_last_blocked_tool": "Edit",
            "consecutive_tool_blocks": 1,
        }

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("_last_blocked_tool") == ""
        assert variables.get("consecutive_tool_blocks") == 0
        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_rule_block_records_tool_name(self, db) -> None:
        """When a rule blocks a BEFORE_TOOL, _last_blocked_tool should be set."""
        # Install a rule that blocks TodoWrite
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        mgr = LocalWorkflowDefinitionManager(db)
        rule_body = {
            "event": "before_tool",
            "effects": [
                {
                    "type": "block",
                    "tools": ["TodoWrite"],
                    "reason": "Use gobby-tasks instead",
                },
            ],
        }
        mgr.create(
            name="block-todowrite-test",
            workflow_type="rule",
            definition_json=json.dumps(rule_body),
            source="installed",
            enabled=True,
            priority=10,
        )

        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "block"
        assert variables.get("tool_block_pending") is not True
        assert variables.get("_last_blocked_tool") == "TodoWrite"

    @pytest.mark.asyncio
    async def test_rule_block_does_not_trigger_tool_failure_recovery_on_stop(self, db) -> None:
        """A BEFORE_TOOL gate block should not make the next STOP look like a tool failure."""
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        mgr = LocalWorkflowDefinitionManager(db)
        rule_body = {
            "event": "before_tool",
            "effects": [
                {
                    "type": "block",
                    "tools": ["TodoWrite"],
                    "reason": "Use gobby-tasks instead",
                },
            ],
        }
        mgr.create(
            name="block-todowrite-stop-test",
            workflow_type="rule",
            definition_json=json.dumps(rule_body),
            source="installed",
            enabled=True,
            priority=10,
        )

        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        blocked_event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        blocked = await engine.evaluate(blocked_event, "sess-1", variables)
        assert blocked.decision == "block"
        assert variables.get("_last_blocked_tool") == "TodoWrite"
        assert variables.get("tool_block_pending") is not True

        stop_event = _make_event(HookEventType.STOP)
        stop = await engine.evaluate(stop_event, "sess-1", variables)

        assert stop.decision == "allow"
        assert "tool-failure-recovery" not in (stop.reason or "")

    @pytest.mark.asyncio
    async def test_death_spiral_scenario_recoverable(self, db) -> None:
        """End-to-end: the exact death spiral scenario is now recoverable.

        1. TodoWrite blocked by rule → pending
        2. Edit attempted → counter resets (different tool), proceeds
        3. Read attempted → still works
        4. Agent can recover instead of being stuck
        """
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        mgr = LocalWorkflowDefinitionManager(db)
        rule_body = {
            "event": "before_tool",
            "effects": [
                {
                    "type": "block",
                    "tools": ["TodoWrite"],
                    "reason": "Use gobby-tasks instead",
                },
            ],
        }
        mgr.create(
            name="block-todowrite-test",
            workflow_type="rule",
            definition_json=json.dumps(rule_body),
            source="installed",
            enabled=True,
            priority=10,
        )

        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. TodoWrite blocked
        todo_event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        r1 = await engine.evaluate(todo_event, "sess-1", variables)
        assert r1.decision == "block"
        assert variables.get("_last_blocked_tool") == "TodoWrite"

        # 2. Edit attempted — different tool, counter resets, allowed
        edit_event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )
        r2 = await engine.evaluate(edit_event, "sess-1", variables)
        assert r2.decision == "allow"
        assert variables.get("consecutive_tool_blocks") == 0

        # 3. Read attempted — also allowed
        read_event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
        )
        r3 = await engine.evaluate(read_event, "sess-1", variables)
        assert r3.decision == "allow"


# ---------------------------------------------------------------------------
# Claimed task reconciliation on STOP
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    status: str = "in_progress",
    assignee: str | None = "sess-1",
):
    """Create a minimal Task dataclass for reconciliation tests."""
    from gobby.storage.tasks import Task

    stage_state = {
        "open": "ready",
        "closed": "done",
        "escalated": "ready",
    }.get(status, status)
    return Task(
        id=task_id,
        project_id="proj-1",
        title="Test task",
        priority=1,
        task_type="task",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        closed_at="2026-01-02T00:00:00Z" if status == "closed" else None,
        assignee=assignee,
        escalated_at="2026-01-02T00:00:00Z" if status == "escalated" else None,
        is_escalated=status == "escalated",
        stages=(
            {
                "stage_name": "development",
                "position": 0,
                "state": stage_state,
            },
        ),
    )


class TestClaimedTaskReconciliation:
    """Test reconcile_claimed_tasks() fixes false positives on STOP."""

    def test_reconcile_fixes_inconsistent_boolean(self) -> None:
        """task_claimed=True with empty claimed_tasks → corrected to False."""
        from gobby.workflows.observers import reconcile_claimed_tasks

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, "sess-1")

        assert variables["task_claimed"] is False

    def test_reconcile_noop_when_both_falsy(self) -> None:
        """No changes when task_claimed is already False and dict is empty."""
        from gobby.workflows.observers import reconcile_claimed_tasks

        variables: dict[str, object] = {
            "task_claimed": False,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, "sess-1")

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}

    def test_reconcile_prunes_closed_tasks(self) -> None:
        """Task in dict that is closed in DB → pruned, task_claimed=False."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task("uuid-1", status="closed")

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}

    def test_reconcile_prunes_reassigned_tasks(self) -> None:
        """Task assigned to a different session → pruned."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task(
            "uuid-1", status="in_progress", assignee="other-session"
        )

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}

    def test_reconcile_prunes_deleted_tasks(self) -> None:
        """Task not found in DB → pruned."""
        from unittest.mock import MagicMock

        from gobby.storage.tasks import TaskNotFoundError
        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.side_effect = TaskNotFoundError("gone")

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}

    def test_reconcile_preserves_valid_claims(self) -> None:
        """Task still in_progress + assigned to this session → survives."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task(
            "uuid-1", status="in_progress", assignee="sess-1"
        )

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-1": "#10"}

    def test_reconcile_preserves_review_claims(self) -> None:
        """needs_review + assigned to this session should still count as claimed work."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task(
            "uuid-review",
            status="needs_review",
            assignee="sess-1",
        )

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-review": "#11"},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-review": "#11"}

    def test_reconcile_mixed_valid_and_stale(self) -> None:
        """Mix of valid and stale claims → only valid ones survive."""
        from unittest.mock import MagicMock

        from gobby.storage.tasks import TaskNotFoundError
        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()

        def get_task_side_effect(task_id):
            if task_id == "uuid-valid":
                return _make_task("uuid-valid", status="in_progress", assignee="sess-1")
            elif task_id == "uuid-closed":
                return _make_task("uuid-closed", status="closed", assignee="sess-1")
            else:
                raise TaskNotFoundError("gone")

        task_manager.get_task.side_effect = get_task_side_effect

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {
                "uuid-valid": "#1",
                "uuid-closed": "#2",
                "uuid-deleted": "#3",
            },
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-valid": "#1"}

    def test_reconcile_no_task_manager(self) -> None:
        """Graceful skip when task_manager is unavailable — no crash, no changes."""
        from gobby.workflows.observers import reconcile_claimed_tasks

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=None)

        # Should not modify — can't verify without DB
        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-1": "#10"}

    def test_reconcile_rebuilds_from_db_when_dict_empty(self) -> None:
        """Empty dict + task_manager → should rebuild claimed_tasks from DB."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        db_task = _make_task("uuid-db", status="in_progress", assignee="sess-1")
        db_task.seq_num = 42
        task_manager.list_tasks.return_value = [db_task]

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-db": "#42"}
        task_manager.list_tasks.assert_called_once()
        call_kwargs = task_manager.list_tasks.call_args.kwargs
        assert call_kwargs["claimed_by_session_id"] == "sess-1"
        assert set(call_kwargs["current_stage_state"]) == set(ACTIVE_STAGE_STATES)

    def test_reconcile_rebuilds_with_no_seq_num(self) -> None:
        """DB task without seq_num should use truncated UUID as ref."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        db_task = _make_task(
            "abcdef12-3456-7890-abcd-ef1234567890", status="in_progress", assignee="sess-1"
        )
        db_task.seq_num = None
        task_manager.list_tasks.return_value = [db_task]

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"abcdef12-3456-7890-abcd-ef1234567890": "abcdef12"}

    def test_reconcile_rebuilds_review_claims_from_db(self) -> None:
        """Empty dict + DB needs_review task should rebuild claimed state."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        db_task = _make_task("uuid-review-db", status="needs_review", assignee="sess-1")
        db_task.seq_num = 77
        task_manager.list_tasks.return_value = [db_task]

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-review-db": "#77"}

    def test_reconcile_clears_when_db_has_no_tasks(self) -> None:
        """Empty dict + DB confirms no tasks → task_claimed should be False."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.list_tasks.return_value = []

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, "sess-1", task_manager=task_manager)

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}


class TestForceAllowStopWithTaskClaimed:
    """Test that force_allow_stop does NOT bypass require-task-close when task_claimed=True."""

    @pytest.mark.asyncio
    async def test_force_allow_suppressed_when_task_claimed(self, db) -> None:
        """force_allow_stop with task_claimed=True should NOT override to allow."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
            "stop_attempts": 0,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, "sess-1", variables)

        # force_allow_stop should be cleared but NOT set override_decision
        assert variables.get("force_allow_stop") is False
        # Without a require-task-close rule installed, response is allow
        # (the override was suppressed, rule eval proceeds normally)
        # The key assertion: force_allow_stop was consumed without setting override
        assert response.decision == "allow"  # No rules loaded = allow

    @pytest.mark.asyncio
    async def test_force_allow_works_without_task_claimed(self, db) -> None:
        """force_allow_stop without task_claimed should still override to allow."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "tool_block_pending": True,
            "task_claimed": False,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, "sess-1", variables)

        assert response.decision == "allow"
        assert variables.get("force_allow_stop") is False

    @pytest.mark.asyncio
    async def test_force_allow_stop_is_still_cleared_when_suppressed(self, db) -> None:
        """force_allow_stop should be self-clearing even when suppressed by task_claimed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("force_allow_stop") is False

        # Second stop — force_allow_stop is gone, no double-clearing
        variables["force_allow_stop"] = False
        await engine.evaluate(event, "sess-1", variables)
        assert variables.get("force_allow_stop") is False
