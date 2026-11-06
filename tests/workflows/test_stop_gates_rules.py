"""Tests for stop-gates rules and hardcoded engine plumbing.

Tier 1 behaviors (hardcoded in RuleEngine.evaluate):
- stop_attempts auto-increment on STOP except during legitimate waits
- BEFORE_AGENT full reset (tool_block_pending, stop_attempts, etc.)
- tool_block_pending stop gate, force_allow_stop bypass, consecutive tool block counter

Tier 2 rules (YAML templates — configurable):
- synthetic task-close admission
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import Task
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from tests.workflows.rule_arming import (
    SYNTHETIC_ESCALATION_PREFIX,
    SYNTHETIC_RETRY_LIMIT_RULE,
    install_retry_limit_rule,
)

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _insert_rule(
    manager: RuleDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
    priority: int = 100,
) -> str:
    """Insert a test rule into the database."""
    # Unmarked sessions resolve the default agent, which selects rules by tag:default.
    row = manager.create(
        name=name,
        tags=["default"],
        definition_json=body.model_dump_json(),
        priority=priority,
        enabled=True,
    )
    return row.id


def _claimed_task_variables() -> dict[str, object]:
    return {
        "mode_level": 2,
        "task_claimed": True,
        "claimed_tasks": {"task-123": "#1"},
        "stop_attempts": 0,
    }


COMPACT_TURN_END_BYPASS_PENDING = "_compact_turn_end_bypass_pending"


class TestStopAttemptsPlumbing:
    """Test hardcoded stop_attempts increment in RuleEngine.

    stop_attempts increments on ordinary STOP events before gate checks.
    It resets on BEFORE_AGENT (new user turn).
    """

    @pytest.mark.asyncio
    async def test_stop_increments_stop_attempts(self, db: HubDatabase) -> None:
        """STOP event should auto-increment stop_attempts."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("stop_attempts") == 1

    @pytest.mark.asyncio
    async def test_stop_increments_from_existing_value(self, db: HubDatabase) -> None:
        """stop_attempts should increment from current value."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"stop_attempts": 3}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("stop_attempts") == 4

    @pytest.mark.asyncio
    async def test_turn_start_boundary_resets_stop_attempts(self, db: HubDatabase) -> None:
        """The semantic turn_start boundary should reset stop_attempts to 0."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"stop_attempts": 5}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("stop_attempts") == 0

    @pytest.mark.asyncio
    async def test_blocked_stop_feedback_keeps_attempt_count(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True, "stop_attempts": 0}

        blocked = await engine.evaluate(_make_event(HookEventType.STOP), SESSION_ID, variables)
        assert blocked.decision == "block"
        assert variables["stop_attempts"] == 1

        await engine.evaluate(_make_event(HookEventType.BEFORE_AGENT), SESSION_ID, variables)
        assert variables["stop_attempts"] == 1

    @pytest.mark.asyncio
    async def test_stop_attempts_increments_even_when_force_allowed(self, db: HubDatabase) -> None:
        """stop_attempts should increment even when stop is force-allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "stop_attempts": 2,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "allow"
        assert variables.get("stop_attempts") == 3

    @pytest.mark.asyncio
    async def test_stop_attempts_increments_even_when_blocked(self, db: HubDatabase) -> None:
        """stop_attempts should increment even when stop is blocked by tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "stop_attempts": 1,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "block"
        assert variables.get("stop_attempts") == 2


class TestBeforeAgentResetsPlumbing:
    """Test hardcoded BEFORE_AGENT resets in RuleEngine.

    The semantic turn_start boundary clears per-turn stop-cycle state: tool_block_pending,
    stop_attempts, consecutive_tool_blocks, _last_blocked_tool.
    """

    @pytest.mark.asyncio
    async def test_clears_tool_block_pending(self, db: HubDatabase) -> None:
        """The semantic turn_start boundary should clear tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_full_reset_on_new_turn(self, db: HubDatabase) -> None:
        """The semantic turn_start boundary should reset stop-cycle variables."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "stop_attempts": 5,
            "consecutive_tool_blocks": 2,
            "_last_blocked_tool": "Edit",
            "_last_blocked_rule_name": "block-edit",
            "_last_blocked_reason": "Rule enforced by Gobby: [block-edit]\nNo editing",
        }

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables["tool_block_pending"] is False
        assert variables["stop_attempts"] == 0
        assert variables["consecutive_tool_blocks"] == 0
        assert variables["_last_blocked_tool"] == ""
        assert variables["_last_blocked_rule_name"] == ""
        assert variables["_last_blocked_reason"] == ""


def _make_event(
    event_type: HookEventType,
    data: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    """Helper to create HookEvent for rule engine tests."""
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
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
    async def test_after_tool_failure_sets_tool_block_pending(self, db: HubDatabase) -> None:
        """Failed after_tool should auto-set tool_block_pending=True."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Edit", "is_error": True},
        )
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("tool_block_pending") is True

    @pytest.mark.asyncio
    async def test_after_tool_failure_via_metadata(self, db: HubDatabase) -> None:
        """Failed after_tool via metadata.is_failure should also set flag."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Edit"},
            metadata={"is_failure": True},
        )
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("tool_block_pending") is True

    @pytest.mark.asyncio
    async def test_successful_after_tool_clears_tool_block_pending(self, db: HubDatabase) -> None:
        """Successful after_tool should clear tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_stop_blocked_when_tool_block_pending(self, db: HubDatabase) -> None:
        """Stop should be blocked when tool_block_pending is true."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "block"
        assert "tool just failed" in (response.reason or "").lower()

    @pytest.mark.asyncio
    async def test_stop_block_is_self_clearing(self, db: HubDatabase) -> None:
        """After blocking stop, tool_block_pending should be cleared."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"tool_block_pending": True}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_stop_allowed_without_tool_block_pending(self, db: HubDatabase) -> None:
        """Stop should be allowed when tool_block_pending is not set."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_full_cycle_failure_then_stop_then_recovery(self, db: HubDatabase) -> None:
        """End-to-end: tool fails → stop blocked → tool succeeds → stop allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. Tool fails
        fail_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Edit", "is_error": True},
        )
        await engine.evaluate(fail_event, SESSION_ID, variables)
        assert variables.get("tool_block_pending") is True

        # 2. Stop is blocked (and self-clears)
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, SESSION_ID, variables)
        assert response.decision == "block"
        assert variables.get("tool_block_pending") is False

        # 3. Tool succeeds
        ok_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )
        await engine.evaluate(ok_event, SESSION_ID, variables)

        # 4. Stop is allowed
        response = await engine.evaluate(stop_event, SESSION_ID, variables)
        assert response.decision == "allow"


class TestBashErrorStopGate:
    """End-to-end: Bash error detected via is_error → tool_block_pending → stop blocked.

    This tests the full flow that was broken before normalization Phase 3:
    Claude Code sends post-tool-use (not post-tool-use-failure) for Bash errors,
    so is_error must be inferred from tool output content.
    """

    @pytest.mark.asyncio
    async def test_bash_error_via_is_error_blocks_stop(self, db: HubDatabase) -> None:
        """AFTER_TOOL with Bash is_error=True → tool_block_pending → STOP blocked."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. Bash tool fails (is_error set by normalization Phase 3)
        fail_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash", "is_error": True, "tool_output": "Exit code: 1"},
        )
        await engine.evaluate(fail_event, SESSION_ID, variables)
        assert variables.get("tool_block_pending") is True

        # 2. Stop is blocked
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, SESSION_ID, variables)
        assert response.decision == "block"
        assert "tool just failed" in (response.reason or "").lower()

    @pytest.mark.asyncio
    async def test_bash_success_does_not_block_stop(self, db: HubDatabase) -> None:
        """AFTER_TOOL with Bash success → no tool_block_pending → stop allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        ok_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash"},
        )
        await engine.evaluate(ok_event, SESSION_ID, variables)

        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, SESSION_ID, variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_full_bash_failure_recovery_cycle(self, db: HubDatabase) -> None:
        """End-to-end: Bash fails → stop blocked → Bash succeeds → stop allowed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        # 1. Bash fails
        fail_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash", "is_error": True},
        )
        await engine.evaluate(fail_event, SESSION_ID, variables)
        assert variables.get("tool_block_pending") is True

        # 2. Stop blocked (self-clears)
        stop_event = _make_event(HookEventType.STOP)
        r1 = await engine.evaluate(stop_event, SESSION_ID, variables)
        assert r1.decision == "block"

        # 3. Bash succeeds
        ok_event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash"},
        )
        await engine.evaluate(ok_event, SESSION_ID, variables)

        # 4. Stop allowed
        r2 = await engine.evaluate(stop_event, SESSION_ID, variables)
        assert r2.decision == "allow"


class TestForceAllowStop:
    """Test force_allow_stop catastrophic failure bypass.

    force_allow_stop bypasses all stop gates (including tool_block_pending)
    and is self-clearing after one use.
    """

    @pytest.mark.asyncio
    async def test_force_allow_stop_bypasses_stop_gates(self, db: HubDatabase) -> None:
        """force_allow_stop should allow stop even with tool_block_pending."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "tool_block_pending": True,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_force_allow_stop_is_self_clearing(self, db: HubDatabase) -> None:
        """force_allow_stop should be cleared after use."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {"force_allow_stop": True}

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("force_allow_stop") is False

    @pytest.mark.asyncio
    async def test_force_allow_stop_only_works_once(self, db: HubDatabase) -> None:
        """Second stop after force_allow_stop should use normal logic."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "tool_block_pending": True,
        }

        event = _make_event(HookEventType.STOP)

        # First stop — force allowed
        response = await engine.evaluate(event, SESSION_ID, variables)
        assert response.decision == "allow"

        # Re-set tool_block_pending (force_allow_stop is cleared now)
        variables["tool_block_pending"] = True

        # Second stop — normal blocking applies
        response = await engine.evaluate(event, SESSION_ID, variables)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_catastrophic_failure_sets_force_allow_stop(self, db: HubDatabase) -> None:
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
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("force_allow_stop") is True

    @pytest.mark.asyncio
    async def test_catastrophic_rate_limit_detected(self, db: HubDatabase) -> None:
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
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("force_allow_stop") is True

    @pytest.mark.asyncio
    async def test_normal_failure_does_not_set_force_allow_stop(self, db: HubDatabase) -> None:
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
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("force_allow_stop") is not True

    @pytest.mark.asyncio
    async def test_catastrophic_then_stop_allowed(self, db: HubDatabase) -> None:
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
        await engine.evaluate(fail_event, SESSION_ID, variables)
        assert variables.get("force_allow_stop") is True

        # 2. Stop is allowed (bypasses tool_block_pending too)
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, SESSION_ID, variables)
        assert response.decision == "allow"
        assert variables.get("force_allow_stop") is False


class TestConsecutiveBlockScoping:
    """Test that consecutive tool block counter is scoped to the same tool.

    The death spiral fix: when Tool A is blocked, only retries of Tool A
    escalate the counter. Attempting a different Tool B resets the counter
    and proceeds to normal rule evaluation, allowing the agent to recover.

    Each retry below closes its assistant response with a tool-batch event, the
    boundary that separates a genuine retry from a parallel sibling.
    """

    @pytest.mark.asyncio
    async def test_same_tool_retried_5x_escalates(self, db: HubDatabase) -> None:
        """Same tool retried 5 times should hit the configured escalation block."""
        install_retry_limit_rule(db)
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "_last_blocked_rule_name": "block-todowrite",
            "_last_blocked_reason": (
                "Rule enforced by Gobby: [block-todowrite]\nUse gobby-tasks instead"
            ),
            "max_consecutive_blocked_tool_attempts": 5,
        }

        async def close_batch() -> None:
            await engine.evaluate(
                _make_event(HookEventType.POST_TOOL_BATCH, data={"tool_calls": []}),
                SESSION_ID,
                variables,
            )

        # Attempt 1: counter goes to 1, no escalation yet
        event1 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response1 = await engine.evaluate(event1, SESSION_ID, variables)
        assert variables.get("consecutive_tool_blocks") == 1
        # Not escalated yet — passes through to rule evaluation
        # (no rules installed, so it allows)
        assert response1.decision == "allow"
        await close_batch()

        # Re-set blocked-tool tracking (simulates the rule blocking it again)
        variables["_last_blocked_tool"] = "TodoWrite"

        # Attempt 2: counter goes to 2, still below threshold
        event2 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response2 = await engine.evaluate(event2, SESSION_ID, variables)
        assert variables.get("consecutive_tool_blocks") == 2
        assert response2.decision == "allow"
        await close_batch()

        variables["_last_blocked_tool"] = "TodoWrite"

        # Attempt 3: counter goes to 3, still below threshold
        event3 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response3 = await engine.evaluate(event3, SESSION_ID, variables)
        assert variables.get("consecutive_tool_blocks") == 3
        assert response3.decision == "allow"
        await close_batch()

        variables["_last_blocked_tool"] = "TodoWrite"

        # Attempt 4: counter goes to 4 → escalation block on the 5th total attempt
        event4 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "TodoWrite"},
        )
        response4 = await engine.evaluate(event4, SESSION_ID, variables)
        assert variables.get("consecutive_tool_blocks") == 4
        assert response4.decision == "block"
        assert response4.reason is not None
        assert response4.reason.startswith(
            f"Rule enforced by Gobby: [{SYNTHETIC_RETRY_LIMIT_RULE}]\n"
            f"{SYNTHETIC_ESCALATION_PREFIX}: TodoWrite x5 | "
            "Rule enforced by Gobby: [block-todowrite]\nUse gobby-tasks instead"
        )

    @pytest.mark.asyncio
    async def test_different_tool_resets_counter(self, db: HubDatabase) -> None:
        """Different tool after a block should reset counter and proceed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "_last_blocked_rule_name": "block-todowrite",
            "_last_blocked_reason": (
                "Rule enforced by Gobby: [block-todowrite]\nUse gobby-tasks instead"
            ),
            "consecutive_tool_blocks": 1,
        }

        # Try a different tool (Read) — counter should reset
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
        )
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("consecutive_tool_blocks") == 0
        assert variables.get("_last_blocked_tool") == "TodoWrite"
        assert variables.get("_last_blocked_rule_name") == "block-todowrite"
        assert variables.get("_last_blocked_reason") == (
            "Rule enforced by Gobby: [block-todowrite]\nUse gobby-tasks instead"
        )
        # No rules installed, so it allows through to normal evaluation
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_pipeline_direct_mcp_retry_clears_stale_counter(self, db: HubDatabase) -> None:
        """Pipeline-scoped direct MCP calls should not inherit interactive retry state."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "gobby-workflows:list_pipeline_executions",
            "_last_blocked_rule_name": "pipeline-block",
            "_last_blocked_reason": "Rule enforced by Gobby: [pipeline-block]\nRetry later",
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
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "allow"
        assert variables.get("consecutive_tool_blocks") == 0
        assert variables.get("_last_blocked_tool") == ""
        assert variables.get("_last_blocked_rule_name") == ""
        assert variables.get("_last_blocked_reason") == ""

    @pytest.mark.asyncio
    async def test_different_tool_blocked_starts_own_counter(self, db: HubDatabase) -> None:
        """If a different tool is also rule-blocked, it starts its own counter."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "_last_blocked_rule_name": "block-todowrite",
            "_last_blocked_reason": (
                "Rule enforced by Gobby: [block-todowrite]\nUse gobby-tasks instead"
            ),
            "consecutive_tool_blocks": 1,
        }

        # Edit is a different tool — counter resets to 0
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )
        await engine.evaluate(event, SESSION_ID, variables)
        assert variables.get("consecutive_tool_blocks") == 0

        # Simulate Edit being blocked by a rule (updates last_blocked)
        variables["_last_blocked_tool"] = "Edit"

        # Retry Edit — counter goes to 1
        event2 = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )
        await engine.evaluate(event2, SESSION_ID, variables)
        assert variables.get("consecutive_tool_blocks") == 1

    @pytest.mark.asyncio
    async def test_turn_start_boundary_resets_last_blocked_tool(self, db: HubDatabase) -> None:
        """The semantic turn_start boundary should reset _last_blocked_tool."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "_last_blocked_tool": "TodoWrite",
            "_last_blocked_rule_name": "block-todowrite",
            "_last_blocked_reason": (
                "Rule enforced by Gobby: [block-todowrite]\nUse gobby-tasks instead"
            ),
            "consecutive_tool_blocks": 2,
        }

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("_last_blocked_tool") == ""
        assert variables.get("_last_blocked_rule_name") == ""
        assert variables.get("_last_blocked_reason") == ""
        assert variables.get("consecutive_tool_blocks") == 0

    @pytest.mark.asyncio
    async def test_successful_after_tool_clears_last_blocked(self, db: HubDatabase) -> None:
        """Successful AFTER_TOOL should clear _last_blocked_tool."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "tool_block_pending": True,
            "_last_blocked_tool": "Edit",
            "_last_blocked_rule_name": "block-edit",
            "_last_blocked_reason": "Rule enforced by Gobby: [block-edit]\nNo editing",
            "consecutive_tool_blocks": 1,
        }

        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("_last_blocked_tool") == ""
        assert variables.get("_last_blocked_rule_name") == ""
        assert variables.get("_last_blocked_reason") == ""
        assert variables.get("consecutive_tool_blocks") == 0
        assert variables.get("tool_block_pending") is False

    @pytest.mark.asyncio
    async def test_rule_block_records_tool_name(self, db: HubDatabase) -> None:
        """When a rule blocks a BEFORE_TOOL, _last_blocked_tool should be set."""
        # Install a rule that blocks TodoWrite
        from gobby.storage.definitions.rules import RuleDefinitionManager

        mgr = RuleDefinitionManager(db)
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
            tags=["default"],
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
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "block"
        assert variables.get("tool_block_pending") is not True
        assert variables.get("_last_blocked_tool") == "TodoWrite"
        assert variables.get("_last_blocked_rule_name") == "block-todowrite-test"
        assert variables.get("_last_blocked_reason") == (
            "Rule enforced by Gobby: [block-todowrite-test]\nUse gobby-tasks instead"
        )

    @pytest.mark.asyncio
    async def test_rule_block_does_not_trigger_tool_failure_recovery_on_stop(
        self, db: HubDatabase
    ) -> None:
        """A BEFORE_TOOL gate block should not make the next STOP look like a tool failure."""
        from gobby.storage.definitions.rules import RuleDefinitionManager

        mgr = RuleDefinitionManager(db)
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
            tags=["default"],
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
        blocked = await engine.evaluate(blocked_event, SESSION_ID, variables)
        assert blocked.decision == "block"
        assert variables.get("_last_blocked_tool") == "TodoWrite"
        assert variables.get("_last_blocked_rule_name") == "block-todowrite-stop-test"
        assert variables.get("_last_blocked_reason") == (
            "Rule enforced by Gobby: [block-todowrite-stop-test]\nUse gobby-tasks instead"
        )
        assert variables.get("tool_block_pending") is not True

        stop_event = _make_event(HookEventType.STOP)
        stop = await engine.evaluate(stop_event, SESSION_ID, variables)

        assert stop.decision == "allow"
        assert "tool-failure-recovery" not in (stop.reason or "")

    @pytest.mark.asyncio
    async def test_death_spiral_scenario_recoverable(self, db: HubDatabase) -> None:
        """End-to-end: the exact death spiral scenario is now recoverable.

        1. TodoWrite blocked by rule → pending
        2. Edit attempted → counter resets (different tool), proceeds
        3. Read attempted → still works
        4. Agent can recover instead of being stuck
        """
        from gobby.storage.definitions.rules import RuleDefinitionManager

        mgr = RuleDefinitionManager(db)
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
            tags=["default"],
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
        r1 = await engine.evaluate(todo_event, SESSION_ID, variables)
        assert r1.decision == "block"
        assert variables.get("_last_blocked_tool") == "TodoWrite"
        assert variables.get("_last_blocked_rule_name") == "block-todowrite-test"
        assert variables.get("_last_blocked_reason") == (
            "Rule enforced by Gobby: [block-todowrite-test]\nUse gobby-tasks instead"
        )

        # 2. Edit attempted — different tool, counter resets, allowed
        edit_event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )
        r2 = await engine.evaluate(edit_event, SESSION_ID, variables)
        assert r2.decision == "allow"
        assert variables.get("consecutive_tool_blocks") == 0
        assert variables.get("_last_blocked_tool") == "TodoWrite"
        assert variables.get("_last_blocked_rule_name") == "block-todowrite-test"

        # 3. Read attempted — also allowed
        read_event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
        )
        r3 = await engine.evaluate(read_event, SESSION_ID, variables)
        assert r3.decision == "allow"
        assert variables.get("_last_blocked_tool") == "TodoWrite"
        assert variables.get("_last_blocked_rule_name") == "block-todowrite-test"


# ---------------------------------------------------------------------------
# Claimed task reconciliation on STOP
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    status: str = "in_progress",
    claimed_by_session_id: str | None = SESSION_ID,
) -> Task:
    """Create a minimal Task dataclass for reconciliation tests."""
    return Task(
        id=task_id,
        project_id="proj-1",
        title="Test task",
        priority=1,
        task_type="task",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        closed_at=datetime(2026, 1, 2, tzinfo=UTC) if status == "closed" else None,
        claimed_by_session_id=claimed_by_session_id,
        escalated_at=datetime(2026, 1, 2, tzinfo=UTC) if status == "escalated" else None,
        is_escalated=status == "escalated",
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
        reconcile_claimed_tasks(variables, SESSION_ID)

        assert variables["task_claimed"] is False

    def test_reconcile_noop_when_both_falsy(self) -> None:
        """No changes when task_claimed is already False and dict is empty."""
        from gobby.workflows.observers import reconcile_claimed_tasks

        variables: dict[str, object] = {
            "task_claimed": False,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, SESSION_ID)

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
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}

    def test_reconcile_prunes_reassigned_tasks(self) -> None:
        """Task assigned to a different session → pruned."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task(
            "uuid-1", status="in_progress", claimed_by_session_id="other-session"
        )

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

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
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}

    def test_reconcile_preserves_valid_claims(self) -> None:
        """Task still in_progress + assigned to this session → survives."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task(
            "uuid-1", status="in_progress", claimed_by_session_id=SESSION_ID
        )

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

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
            claimed_by_session_id=SESSION_ID,
        )

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-review": "#11"},
        }
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-review": "#11"}

    def test_reconcile_repairs_lineage_claim_instead_of_pruning(self) -> None:
        """A compact/continuation child keeps an active task owned by its parent."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task(
            "uuid-anchor",
            status="in_progress",
            claimed_by_session_id="parent-sess",
        )
        session_manager = MagicMock()
        session_manager.is_ancestor.side_effect = (
            lambda ancestor, descendant: ancestor == "parent-sess" and descendant == "child-sess"
        )
        parent_session = MagicMock()
        parent_session.parent_session_id = None
        parent_session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        child_session = MagicMock()
        child_session.parent_session_id = "parent-sess"
        child_session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        session_manager.get.side_effect = lambda sid: {
            "parent-sess": parent_session,
            "child-sess": child_session,
        }[sid]
        session_task_manager = MagicMock()

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-anchor": "#14853"},
        }
        reconcile_claimed_tasks(
            variables,
            "child-sess",
            task_manager=task_manager,
            session_manager=session_manager,
            session_task_manager=session_task_manager,
        )

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-anchor": "#14853"}
        task_manager.claim_task.assert_called_once_with(
            "uuid-anchor",
            session_id="child-sess",
            force=True,
        )
        session_task_manager.link_task.assert_called_once_with(
            "child-sess",
            "uuid-anchor",
            "claimed",
        )

    def test_reconcile_prunes_lineage_claim_from_different_terminal(self) -> None:
        """A stale child lineage alone cannot preserve a task claim."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        task_manager.get_task.return_value = _make_task(
            "uuid-anchor",
            status="in_progress",
            claimed_by_session_id="parent-sess",
        )
        session_manager = MagicMock()
        session_manager.is_ancestor.side_effect = (
            lambda ancestor, descendant: ancestor == "parent-sess" and descendant == "child-sess"
        )
        parent_session = MagicMock()
        parent_session.parent_session_id = None
        parent_session.terminal_context = {"tmux_pane": "%5815", "tmux_socket_path": "/tmp/tmux"}
        child_session = MagicMock()
        child_session.parent_session_id = "parent-sess"
        child_session.terminal_context = {"tmux_pane": "%5867", "tmux_socket_path": "/tmp/tmux"}
        session_manager.get.side_effect = lambda sid: {
            "parent-sess": parent_session,
            "child-sess": child_session,
        }[sid]

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-anchor": "#14853"},
        }
        reconcile_claimed_tasks(
            variables,
            "child-sess",
            task_manager=task_manager,
            session_manager=session_manager,
        )

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}
        task_manager.claim_task.assert_not_called()

    def test_reconcile_mixed_valid_and_stale(self) -> None:
        """Mix of valid and stale claims → only valid ones survive."""
        from unittest.mock import MagicMock

        from gobby.storage.tasks import TaskNotFoundError
        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()

        def get_task_side_effect(task_id: str) -> Task:
            if task_id == "uuid-valid":
                return _make_task(
                    "uuid-valid", status="in_progress", claimed_by_session_id=SESSION_ID
                )
            elif task_id == "uuid-closed":
                return _make_task("uuid-closed", status="closed", claimed_by_session_id=SESSION_ID)
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
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-valid": "#1"}

    def test_reconcile_no_task_manager(self) -> None:
        """Graceful skip when task_manager is unavailable — no crash, no changes."""
        from gobby.workflows.observers import reconcile_claimed_tasks

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=None)

        # Should not modify — can't verify without DB
        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-1": "#10"}

    def test_reconcile_rebuilds_from_db_when_dict_empty(self) -> None:
        """Empty dict + task_manager → should rebuild claimed_tasks from DB."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        db_task = _make_task("uuid-db", status="in_progress", claimed_by_session_id=SESSION_ID)
        db_task.seq_num = 42
        task_manager.list_tasks.return_value = [db_task]

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"uuid-db": "#42"}
        task_manager.list_tasks.assert_called_once()
        call_kwargs = task_manager.list_tasks.call_args.kwargs
        assert call_kwargs["claimed_by_session_id"] == SESSION_ID
        assert "current_stage_state" not in call_kwargs

    def test_reconcile_rebuilds_with_no_seq_num(self) -> None:
        """DB task without seq_num should use truncated UUID as ref."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        db_task = _make_task(
            "abcdef12-3456-7890-abcd-ef1234567890",
            status="in_progress",
            claimed_by_session_id=SESSION_ID,
        )
        db_task.seq_num = None
        task_manager.list_tasks.return_value = [db_task]

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_tasks"] == {"abcdef12-3456-7890-abcd-ef1234567890": "abcdef12"}

    def test_reconcile_rebuilds_review_claims_from_db(self) -> None:
        """Empty dict + DB needs_review task should rebuild claimed state."""
        from unittest.mock import MagicMock

        from gobby.workflows.observers import reconcile_claimed_tasks

        task_manager = MagicMock()
        db_task = _make_task(
            "uuid-review-db", status="needs_review", claimed_by_session_id=SESSION_ID
        )
        db_task.seq_num = 77
        task_manager.list_tasks.return_value = [db_task]

        variables: dict[str, object] = {
            "task_claimed": True,
            "claimed_tasks": {},
        }
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

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
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=task_manager)

        assert variables["task_claimed"] is False
        assert variables["claimed_tasks"] == {}


class TestForceAllowStopWithTaskClaimed:
    """Test that force_allow_stop does not bypass a claimed task's close requirement."""

    @pytest.mark.asyncio
    async def test_force_allow_suppressed_when_task_claimed(self, db: HubDatabase) -> None:
        """force_allow_stop with task_claimed=True should NOT override to allow."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
            "stop_attempts": 0,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, SESSION_ID, variables)

        # force_allow_stop should be cleared but NOT set override_decision
        assert variables.get("force_allow_stop") is False
        # Without a task-close rule installed, response is allow
        # (the override was suppressed, rule eval proceeds normally)
        # The key assertion: force_allow_stop was consumed without setting override
        assert response.decision == "allow"  # No rules loaded = allow

    @pytest.mark.asyncio
    async def test_force_allow_works_without_task_claimed(self, db: HubDatabase) -> None:
        """force_allow_stop without task_claimed should still override to allow."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "tool_block_pending": True,
            "task_claimed": False,
        }

        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, SESSION_ID, variables)

        assert response.decision == "allow"
        assert variables.get("force_allow_stop") is False

    @pytest.mark.asyncio
    async def test_force_allow_stop_is_still_cleared_when_suppressed(self, db: HubDatabase) -> None:
        """force_allow_stop should be self-clearing even when suppressed by task_claimed."""
        engine = RuleEngine(db)
        variables: dict[str, object] = {
            "force_allow_stop": True,
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#10"},
        }

        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, SESSION_ID, variables)

        assert variables.get("force_allow_stop") is False

        # Second stop — force_allow_stop is gone, no double-clearing
        variables["force_allow_stop"] = False
        await engine.evaluate(event, SESSION_ID, variables)
        assert variables.get("force_allow_stop") is False
