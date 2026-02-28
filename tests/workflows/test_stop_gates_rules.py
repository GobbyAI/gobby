"""Tests for stop-gates rules and hardcoded engine plumbing.

Tier 1 behaviors (hardcoded in RuleEngine.evaluate):
- stop_attempts auto-increment on STOP
- BEFORE_AGENT full reset (tool_block_pending, pre_existing_errors_triaged, stop_attempts, etc.)
- tool_block_pending stop gate, force_allow_stop bypass, consecutive tool block counter

Tier 2 rules (YAML templates — configurable):
- require-error-triage, require-task-close
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.rule_engine import RuleEngine
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync import sync_bundled_rules

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
    from gobby.workflows.sync import get_bundled_rules_path

    return sync_bundled_rules(db, get_bundled_rules_path())


def _get_rule(manager, name):
    """Get a bundled rule by name (templates included since bundled rules are templates)."""
    return manager.get_by_name(name, include_templates=True)


STOP_GATES_RULES = {
    "require-error-triage",
    "require-task-close",
}


class TestStopGatesSync:
    """Test that stop-gates rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All stop-gates rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert STOP_GATES_RULES.issubset(rule_names), (
            f"Missing: {STOP_GATES_RULES - rule_names}"
        )

    def test_all_rules_have_group(self, db, manager) -> None:
        """All rules should have group='stop-gates'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in STOP_GATES_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "stop-gates", (
                    f"{row.name} missing group"
                )

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
    async def test_before_agent_resets_stop_attempts(self, db) -> None:
        """BEFORE_AGENT should reset stop_attempts to 0."""
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
    """Verify require-error-triage blocks stop until triage confirmed."""

    def test_blocks_on_stop(self, db, manager) -> None:
        """Should be a block effect on stop event."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-error-triage")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "stop"
        assert body.effect.type == "block"

    def test_when_checks_triage_flag(self, db, manager) -> None:
        """Should check pre_existing_errors_triaged and task_has_commits."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-error-triage")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "pre_existing_errors_triaged" in body.when
        assert "task_has_commits" in body.when


class TestRequireTaskClose:
    """Verify require-task-close blocks stop if task in_progress."""

    def test_blocks_on_stop(self, db, manager) -> None:
        """Should be a block effect on stop event."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-task-close")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "stop"
        assert body.effect.type == "block"

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
        assert not evaluator.evaluate(body.when), (
            "Rule should not fire when task_claimed is unset"
        )

    def test_blocks_when_task_claimed_is_set(self, db, manager) -> None:
        """Should block when task_claimed is set and conditions met."""
        _sync_bundled(db)

        row = _get_rule(manager, "require-task-close")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        variables: dict[str, object] = {
            "mode_level": 2,
            "stop_attempts": 1,
            "task_claimed": True,
            "claimed_task_id": "task-123",
        }
        evaluator = SafeExpressionEvaluator(
            context={"variables": variables},
            allowed_funcs={"len": len, "str": str, "int": int, "bool": bool},
        )
        assert evaluator.evaluate(body.when), (
            "Rule should fire when task_claimed is set"
        )


class TestBeforeAgentResetsPlumbing:
    """Test hardcoded BEFORE_AGENT resets in RuleEngine.

    BEFORE_AGENT clears all stop-cycle state: tool_block_pending,
    pre_existing_errors_triaged, stop_attempts, consecutive_tool_blocks,
    _last_blocked_tool.
    """

    @pytest.mark.asyncio
    async def test_clears_tool_block_pending(self, db) -> None:
        """BEFORE_AGENT should clear tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_clears_pre_existing_errors_triaged(self, db) -> None:
        """BEFORE_AGENT should clear pre_existing_errors_triaged."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"pre_existing_errors_triaged": True}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables.get("pre_existing_errors_triaged") is False

    @pytest.mark.asyncio
    async def test_full_reset_on_new_turn(self, db) -> None:
        """BEFORE_AGENT should reset all stop-cycle variables at once."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "pre_existing_errors_triaged": True,
            "stop_attempts": 5,
            "consecutive_tool_blocks": 2,
            "_last_blocked_tool": "Edit",
        }

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, "sess-1", variables)

        assert variables["tool_block_pending"] is False
        assert variables["pre_existing_errors_triaged"] is False
        assert variables["stop_attempts"] == 0
        assert variables["consecutive_tool_blocks"] == 0
        assert variables["_last_blocked_tool"] == ""


def _make_event(
    event_type: HookEventType,
    data: dict | None = None,
    metadata: dict | None = None,
) -> HookEvent:
    """Helper to create HookEvent for rule engine tests."""
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
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
    async def test_same_tool_retried_3x_escalates(self, db) -> None:
        """Same tool retried 3 times should hit the hardcoded escalation block."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "_last_blocked_tool": "TodoWrite",
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

        # Re-set tool_block_pending (simulates the rule blocking it again)
        variables["tool_block_pending"] = True
        variables["_last_blocked_tool"] = "TodoWrite"

        # Attempt 2: counter goes to 2 → escalation block
        event2 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response2 = await engine.evaluate(event2, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 2
        assert response2.decision == "block"
        assert "TodoWrite" in response2.reason
        assert "3 times" in response2.reason

    @pytest.mark.asyncio
    async def test_different_tool_resets_counter(self, db) -> None:
        """Different tool after a block should reset counter and proceed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
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
    async def test_different_tool_blocked_starts_own_counter(self, db) -> None:
        """If a different tool is also rule-blocked, it starts its own counter."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
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

        # Simulate Edit being blocked by a rule (sets pending + last_blocked)
        variables["tool_block_pending"] = True
        variables["_last_blocked_tool"] = "Edit"

        # Retry Edit — counter goes to 1
        event2 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )
        await engine.evaluate(event2, "sess-1", variables)
        assert variables.get("consecutive_tool_blocks") == 1

    @pytest.mark.asyncio
    async def test_before_agent_resets_last_blocked_tool(self, db) -> None:
        """BEFORE_AGENT should reset _last_blocked_tool alongside the counter."""
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
            "effect": {
                "type": "block",
                "tools": ["TodoWrite"],
                "reason": "Use gobby-tasks instead",
            },
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
        assert variables.get("tool_block_pending") is True
        assert variables.get("_last_blocked_tool") == "TodoWrite"

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
            "effect": {
                "type": "block",
                "tools": ["TodoWrite"],
                "reason": "Use gobby-tasks instead",
            },
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
