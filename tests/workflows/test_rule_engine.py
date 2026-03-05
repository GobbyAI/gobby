"""Tests for RuleEngine single-pass evaluation loop."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.rule_engine import RuleEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_rule_engine.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=source,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _insert_rule(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
    priority: int = 100,
    enabled: bool = True,
    sources: list[str] | None = None,
) -> str:
    """Helper to insert a rule into the database."""
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=priority,
        enabled=enabled,
        sources=sources,
    )
    return row.id


async def _assert_evaluation(
    db: LocalDatabase,
    event: HookEvent,
    expected_decision: str,
    variables: dict[str, Any] | None = None,
    expected_reason_contains: str | None = None,
    session_id: str = "sess-1",
) -> Any:
    """Helper to evaluate an event and check the decision."""

    engine = RuleEngine(db)
    if variables is None:
        variables = {}
    response = await engine.evaluate(event, session_id=session_id, variables=variables)
    assert response.decision == expected_decision
    if expected_reason_contains:
        assert expected_reason_contains in (response.reason or "")
    return response


class TestRuleEngineLoadRules:
    @pytest.mark.asyncio
    async def test_loads_rules_by_event(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """RuleEngine should load only rules matching the event type."""

        _insert_rule(
            manager,
            "before-tool-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="blocked"),
            ),
        )
        _insert_rule(
            manager,
            "after-tool-rule",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(type="set_variable", variable="x", value=True),
            ),
        )

        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await _assert_evaluation(db, event, "block")

    @pytest.mark.asyncio
    async def test_skips_disabled_rules(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Disabled rules should not be evaluated."""

        _insert_rule(
            manager,
            "disabled-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="should not fire"),
            ),
            enabled=False,
        )

        event = _make_event(HookEventType.BEFORE_TOOL)
        await _assert_evaluation(db, event, "allow")


class TestBlockEffect:
    @pytest.mark.asyncio
    async def test_block_returns_deny(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Block effect should return a block/deny decision."""

        _insert_rule(
            manager,
            "block-edit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="No editing allowed", tools=["Edit"]),
            ),
        )

        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await _assert_evaluation(db, event, "block", expected_reason_contains="No editing allowed")

    @pytest.mark.asyncio
    async def test_block_non_matching_tool_allows(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Block rule with tools filter should not block non-matching tools."""

        _insert_rule(
            manager,
            "block-edit-only",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="No editing", tools=["Edit"]),
            ),
        )

        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        await _assert_evaluation(db, event, "allow")

    @pytest.mark.asyncio
    async def test_first_block_wins(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When multiple rules block, the first (by priority) wins."""

        _insert_rule(
            manager,
            "high-priority-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="First block"),
            ),
            priority=10,
        )
        _insert_rule(
            manager,
            "low-priority-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="Second block"),
            ),
            priority=20,
        )

        event = _make_event(HookEventType.BEFORE_TOOL)
        await _assert_evaluation(db, event, "block", expected_reason_contains="First block")


class TestSetVariableEffect:
    @pytest.mark.asyncio
    async def test_set_variable_literal(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """set_variable with a literal value should update variables."""

        _insert_rule(
            manager,
            "set-claimed",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(type="set_variable", variable="task_claimed", value=True),
            ),
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL)
        await _assert_evaluation(db, event, "allow", variables=variables)

        assert variables.get("task_claimed") is True

    @pytest.mark.asyncio
    async def test_set_variable_expression(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """set_variable with a string expression should evaluate it."""

        _insert_rule(
            manager,
            "increment-counter",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(
                    type="set_variable",
                    variable="custom_counter",
                    value="variables.get('custom_counter', 0) + 1",
                ),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"custom_counter": 2}
        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["custom_counter"] == 3


class TestInjectContextEffect:
    @pytest.mark.asyncio
    async def test_inject_context_adds_system_message(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """inject_context should add template to response context."""

        _insert_rule(
            manager,
            "inject-task-context",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effect=RuleEffect(
                    type="inject_context",
                    template="You are working on an important task.",
                ),
            ),
        )

        event = _make_event(HookEventType.SESSION_START)
        response = await _assert_evaluation(db, event, "allow")

        assert "important task" in (response.context or "")

    @pytest.mark.asyncio
    async def test_multiple_inject_context_accumulate(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Multiple inject_context effects should accumulate."""

        _insert_rule(
            manager,
            "inject-a",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effect=RuleEffect(type="inject_context", template="Context A."),
            ),
            priority=10,
        )
        _insert_rule(
            manager,
            "inject-b",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effect=RuleEffect(type="inject_context", template="Context B."),
            ),
            priority=20,
        )

        event = _make_event(HookEventType.SESSION_START)
        response = await _assert_evaluation(db, event, "allow")

        assert "Context A" in (response.context or "")
        assert "Context B" in (response.context or "")


class TestWhenConditions:
    @pytest.mark.asyncio
    async def test_when_true_fires(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rule with when=True condition should fire."""

        _insert_rule(
            manager,
            "conditional-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="variables.get('require_uv')",
                effect=RuleEffect(type="block", reason="Use uv"),
            ),
        )

        event = _make_event(HookEventType.BEFORE_TOOL)
        await _assert_evaluation(db, event, "block", variables={"require_uv": True})

    @pytest.mark.asyncio
    async def test_when_false_skips(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rule with when=False condition should not fire."""

        _insert_rule(
            manager,
            "conditional-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="variables.get('require_uv')",
                effect=RuleEffect(type="block", reason="Use uv"),
            ),
        )

        event = _make_event(HookEventType.BEFORE_TOOL)
        await _assert_evaluation(db, event, "allow", variables={"require_uv": False})

    @pytest.mark.asyncio
    async def test_when_none_always_fires(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rule without a when condition should always fire."""

        _insert_rule(
            manager,
            "unconditional-set",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="set_variable", variable="stop_attempts", value=0),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"stop_attempts": 5}
        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["stop_attempts"] == 0


class TestPriorityOrdering:
    @pytest.mark.asyncio
    async def test_rules_evaluated_in_priority_order(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules should be evaluated from lowest priority number (highest priority) first."""

        # Priority 20 sets x=1, priority 10 sets x=2
        # Since 10 runs first, then 20 overwrites to 1
        _insert_rule(
            manager,
            "set-x-to-1",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(type="set_variable", variable="x", value=1),
            ),
            priority=20,
        )
        _insert_rule(
            manager,
            "set-x-to-2",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(type="set_variable", variable="x", value=2),
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        # Priority 10 runs first (x=2), then priority 20 (x=1)
        assert variables["x"] == 1


class TestSessionOverrides:
    @pytest.mark.asyncio
    async def test_session_override_disables_rule(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """A session override with enabled=False should skip the rule."""

        _insert_rule(
            manager,
            "block-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="Blocked!"),
            ),
        )

        # Insert session override to disable the rule
        import uuid

        session_id = "override-session"
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), session_id, "block-rule", 0),
        )

        event = _make_event(HookEventType.BEFORE_TOOL)
        await _assert_evaluation(db, event, "allow", session_id=session_id)

    @pytest.mark.asyncio
    async def test_session_override_only_affects_that_session(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Override for one session should not affect another session."""

        _insert_rule(
            manager,
            "block-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="Blocked!"),
            ),
        )

        import uuid

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), "session-a", "block-rule", 0),
        )

        event = _make_event(HookEventType.BEFORE_TOOL)

        # session-b should still be blocked
        await _assert_evaluation(db, event, "block", session_id="session-b")


class TestObserveEffect:
    @pytest.mark.asyncio
    async def test_observe_appends_to_observations(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """observe effect should append an entry to _observations in variables."""

        _insert_rule(
            manager,
            "observe-tool-use",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(
                    type="observe",
                    category="tool_use",
                    message="Tool was used",
                ),
            ),
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        await _assert_evaluation(db, event, "allow", variables=variables)
        assert "_observations" in variables
        obs_list = variables["_observations"]
        assert len(obs_list) == 1
        assert obs_list[0]["category"] == "tool_use"
        assert obs_list[0]["message"] == "Tool was used"
        assert obs_list[0]["rule"] == "observe-tool-use"
        assert "timestamp" in obs_list[0]

    @pytest.mark.asyncio
    async def test_observe_accumulates(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Multiple observe effects should accumulate entries."""

        _insert_rule(
            manager,
            "observe-a",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(type="observe", category="a", message="first"),
            ),
            priority=10,
        )
        _insert_rule(
            manager,
            "observe-b",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(type="observe", category="b", message="second"),
            ),
            priority=20,
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert len(variables["_observations"]) == 2
        assert variables["_observations"][0]["category"] == "a"
        assert variables["_observations"][1]["category"] == "b"

    @pytest.mark.asyncio
    async def test_observe_defaults_category_to_general(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """observe with no category should default to 'general'."""

        _insert_rule(
            manager,
            "observe-no-cat",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(type="observe", message="no category"),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["_observations"][0]["category"] == "general"

    @pytest.mark.asyncio
    async def test_observe_with_template_message(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """observe with Jinja template in message should render it."""

        _insert_rule(
            manager,
            "observe-template",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effect=RuleEffect(
                    type="observe",
                    category="tool_use",
                    message="Used {{ event.data.get('tool_name', 'unknown') }}",
                ),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["_observations"][0]["message"] == "Used Edit"


class TestMcpCallEffect:
    @pytest.mark.asyncio
    async def test_mcp_call_records_pending_call(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """mcp_call effect should record the call for later execution."""

        _insert_rule(
            manager,
            "memory-recall",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_AGENT,
                effect=RuleEffect(
                    type="mcp_call",
                    server="gobby-memory",
                    tool="recall_with_synthesis",
                    arguments={"limit": 5},
                ),
            ),
        )

        event = _make_event(HookEventType.BEFORE_AGENT)
        response = await _assert_evaluation(db, event, "allow")
        # mcp_calls should be recorded in metadata
        assert len(response.metadata.get("mcp_calls", [])) == 1
        call = response.metadata["mcp_calls"][0]
        assert call["server"] == "gobby-memory"
        assert call["tool"] == "recall_with_synthesis"


class TestVariableRebuild:
    @pytest.mark.asyncio
    async def test_later_rules_see_earlier_variable_changes(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """A rule that sets a variable should affect subsequent rule conditions."""

        # Rule at priority 10: set flag = true
        _insert_rule(
            manager,
            "set-flag",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="set_variable", variable="flag", value=True),
            ),
            priority=10,
        )
        # Rule at priority 20: block only if flag is true
        _insert_rule(
            manager,
            "conditional-block",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                when="variables.get('flag')",
                effect=RuleEffect(type="block", reason="Flag was set"),
            ),
            priority=20,
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.STOP)
        await _assert_evaluation(
            db, event, "block", variables=variables, expected_reason_contains="Flag was set"
        )


class TestMcpCallToolUnwrapping:
    """Tests for unwrapping nested MCP call_tool arguments in _build_eval_context."""

    @pytest.mark.asyncio
    async def test_call_tool_unwraps_dict_arguments(self, db: LocalDatabase) -> None:
        """_build_eval_context should unwrap inner arguments for call_tool events."""

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#1", "commit_sha": "abc123"},
                },
            },
        )
        ctx = engine._build_eval_context(event, variables={})
        assert ctx["tool_input"] == {
            "task_id": "#1",
            "commit_sha": "abc123",
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }

    @pytest.mark.asyncio
    async def test_mcp_prefixed_call_tool_unwraps(self, db: LocalDatabase) -> None:
        """_build_eval_context should unwrap for mcp__gobby__call_tool too."""

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#2", "commit_sha": "def456"},
                },
            },
        )
        ctx = engine._build_eval_context(event, variables={})
        assert ctx["tool_input"] == {
            "task_id": "#2",
            "commit_sha": "def456",
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }

    @pytest.mark.asyncio
    async def test_call_tool_unwraps_json_string_arguments(self, db: LocalDatabase) -> None:
        """_build_eval_context should parse JSON string arguments for call_tool."""

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": '{"task_id": "#3", "commit_sha": "ghi789"}',
                },
            },
        )
        ctx = engine._build_eval_context(event, variables={})
        assert ctx["tool_input"] == {
            "task_id": "#3",
            "commit_sha": "ghi789",
            "server_name": "gobby-tasks",
            "tool_name": "close_task",
        }

    @pytest.mark.asyncio
    async def test_regular_tool_not_unwrapped(self, db: LocalDatabase) -> None:
        """_build_eval_context should NOT unwrap arguments for regular tools."""

        engine = RuleEngine(db)
        original_input = {"file_path": "/foo/bar.py", "old_string": "x", "new_string": "y"}
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit", "tool_input": original_input},
        )
        ctx = engine._build_eval_context(event, variables={})
        assert ctx["tool_input"] == original_input

    @pytest.mark.asyncio
    async def test_rule_condition_sees_unwrapped_args(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """End-to-end: a rule checking tool_input.get('commit_sha') should work on inner args."""

        _insert_rule(
            manager,
            "require-commit-before-close",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="not tool_input.get('commit_sha')",
                effect=RuleEffect(
                    type="block",
                    reason="Must provide commit_sha when closing a task",
                    tools=["call_tool"],
                ),
            ),
        )

        engine = RuleEngine(db)

        # With commit_sha → should allow
        event_with = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#1", "commit_sha": "abc123"},
                },
            },
        )
        response = await engine.evaluate(event_with, session_id="sess-1", variables={})
        assert response.decision == "allow"

        # Without commit_sha → should block
        event_without = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#1"},
                },
            },
        )
        response = await engine.evaluate(event_without, session_id="sess-1", variables={})
        assert response.decision == "block"
        assert "commit_sha" in (response.reason or "")


class TestToggleRuleRejectsTemplate:
    def test_toggle_rule_rejects_template(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """toggle_rule should return error for template-only rules."""
        from gobby.mcp_proxy.tools.workflows._rules import toggle_rule

        manager.create(
            name="template-only-rule",
            definition_json=RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="block", reason="template"),
            ).model_dump_json(),
            workflow_type="rule",
            source="template",
        )

        result = toggle_rule(manager, name="template-only-rule", enabled=True)
        assert result["success"] is False
        assert "template" in result["error"]

    def test_toggle_rule_works_for_installed(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """toggle_rule should work for installed rules."""
        from gobby.mcp_proxy.tools.workflows._rules import toggle_rule

        manager.create(
            name="installed-rule",
            definition_json=RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="block", reason="installed"),
            ).model_dump_json(),
            workflow_type="rule",
            source="installed",
            enabled=False,
        )

        result = toggle_rule(manager, name="installed-rule", enabled=True)
        assert result["success"] is True
        assert result["rule"]["enabled"] is True


class TestMultipleEffects:
    """Tests for rules with multiple effects (effects: [...])."""

    @pytest.mark.asyncio
    async def test_block_and_set_variable(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Multi-effect rule with block + set_variable: variable set AND block fires."""

        _insert_rule(
            manager,
            "block-and-set",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(type="set_variable", variable="was_blocked", value=True),
                    RuleEffect(type="block", reason="Blocked with side-effect"),
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "Blocked with side-effect" in (response.reason or "")
        # Non-block effect should have fired before the block
        assert variables.get("was_blocked") is True

    @pytest.mark.asyncio
    async def test_set_variable_and_inject_context(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Multi-effect rule with set_variable + inject_context: both apply."""

        _insert_rule(
            manager,
            "set-and-inject",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effects=[
                    RuleEffect(type="set_variable", variable="initialized", value=True),
                    RuleEffect(type="inject_context", template="Welcome to the session."),
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.SESSION_START)
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert variables.get("initialized") is True
        assert "Welcome" in (response.context or "")

    @pytest.mark.asyncio
    async def test_per_effect_when_skips_false(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Per-effect when=false should skip that effect but fire others."""

        _insert_rule(
            manager,
            "conditional-effects",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effects=[
                    RuleEffect(
                        type="set_variable",
                        variable="always_set",
                        value=True,
                    ),
                    RuleEffect(
                        type="set_variable",
                        variable="conditionally_set",
                        value=True,
                        when="variables.get('should_set')",
                    ),
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"should_set": False}
        event = _make_event(HookEventType.SESSION_START)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("always_set") is True
        assert variables.get("conditionally_set") is None

    @pytest.mark.asyncio
    async def test_per_effect_when_fires_true(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Per-effect when=true should fire that effect."""

        _insert_rule(
            manager,
            "conditional-effects",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effects=[
                    RuleEffect(
                        type="set_variable",
                        variable="always_set",
                        value=True,
                    ),
                    RuleEffect(
                        type="set_variable",
                        variable="conditionally_set",
                        value=True,
                        when="variables.get('should_set')",
                    ),
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"should_set": True}
        event = _make_event(HookEventType.SESSION_START)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("always_set") is True
        assert variables.get("conditionally_set") is True

    def test_validation_both_effect_and_effects_error(self) -> None:
        """Specifying both effect and effects should raise ValidationError."""
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="not both"):
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="set_variable", variable="x", value=1),
                effects=[RuleEffect(type="set_variable", variable="y", value=2)],
            )

    def test_validation_neither_effect_nor_effects_error(self) -> None:
        """Specifying neither effect nor effects should raise ValidationError."""
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="either"):
            RuleDefinitionBody(event=RuleEvent.STOP)

    def test_validation_multiple_block_effects_error(self) -> None:
        """Multiple block effects in effects list should raise ValidationError."""
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="one.*block"):
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(type="block", reason="first"),
                    RuleEffect(type="block", reason="second"),
                ],
            )

    @pytest.mark.asyncio
    async def test_backward_compat_single_effect(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Existing single-effect rules should continue to work unchanged."""

        _insert_rule(
            manager,
            "legacy-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="Legacy block"),
            ),
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "block"
        assert "Legacy block" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_multiple_mcp_calls(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Multi-effect rule with multiple mcp_call effects should record all."""

        _insert_rule(
            manager,
            "multi-mcp",
            RuleDefinitionBody(
                event=RuleEvent.PRE_COMPACT,
                effects=[
                    RuleEffect(type="mcp_call", server="gobby-tasks", tool="sync_export"),
                    RuleEffect(type="mcp_call", server="gobby-memory", tool="sync_export"),
                ],
            ),
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.PRE_COMPACT)
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        calls = response.metadata.get("mcp_calls", [])
        assert len(calls) == 2
        assert calls[0]["server"] == "gobby-tasks"
        assert calls[1]["server"] == "gobby-memory"


class TestToolBlockPending:
    """Tests for automatic tool_block_pending on before_tool blocks."""

    @pytest.mark.asyncio
    async def test_tool_block_pending_set_on_before_tool_block(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending should be auto-set when a before_tool block fires."""

        _insert_rule(
            manager,
            "block-edit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="No editing", tools=["Edit"]),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("tool_block_pending") is True

    @pytest.mark.asyncio
    async def test_tool_block_pending_not_set_on_stop_block(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending should NOT be set on stop event blocks."""

        _insert_rule(
            manager,
            "block-stop",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="block", reason="Cannot stop yet"),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("tool_block_pending") is None

    @pytest.mark.asyncio
    async def test_tool_block_pending_with_multi_effect(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending should be set even in multi-effect rules with block."""

        _insert_rule(
            manager,
            "multi-with-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(type="set_variable", variable="x", value=42),
                    RuleEffect(type="block", reason="Blocked"),
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("tool_block_pending") is True
        assert variables.get("x") == 42

    @pytest.mark.asyncio
    async def test_tool_block_pending_cleared_on_successful_after_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending should be auto-cleared by the engine on successful after_tool."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"tool_block_pending": True}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["tool_block_pending"] is False

    @pytest.mark.asyncio
    async def test_tool_block_pending_not_cleared_on_failed_after_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending should NOT be cleared on failed after_tool."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"tool_block_pending": True}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        event.metadata["is_failure"] = True
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["tool_block_pending"] is True


class TestConsecutiveToolBlocks:
    """Tests for consecutive tool block detection (engine-level safety)."""

    @pytest.mark.asyncio
    async def test_counter_increments_on_consecutive_block(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Counter should increment when tool_block_pending is already true."""

        _insert_rule(
            manager,
            "block-edit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="No editing", tools=["Edit"]),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"tool_block_pending": True, "_last_blocked_tool": "Edit"}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 1

    @pytest.mark.asyncio
    async def test_short_circuit_fires_at_threshold(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """At count >= 2, engine should short-circuit block without evaluating rules."""

        # Insert a rule with set_variable to prove it never runs
        _insert_rule(
            manager,
            "block-edit-with-side-effect",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(type="set_variable", variable="rule_ran", value=True),
                    RuleEffect(type="block", reason="No editing", tools=["Edit"]),
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "tool_block_pending": True,
            "consecutive_tool_blocks": 1,
            "_last_blocked_tool": "Edit",
        }
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "3 times consecutively" in response.reason
        assert "STOP retrying" in response.reason
        # Rule should NOT have been evaluated — no side effect
        assert variables.get("rule_ran") is None

    @pytest.mark.asyncio
    async def test_counter_resets_on_successful_after_tool(self, db: LocalDatabase) -> None:
        """Counter should reset to 0 on successful after_tool."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "tool_block_pending": True,
            "consecutive_tool_blocks": 3,
        }
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 0

    @pytest.mark.asyncio
    async def test_counter_resets_on_before_agent(self, db: LocalDatabase) -> None:
        """Counter should reset to 0 on BEFORE_AGENT (new turn = fresh start)."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"consecutive_tool_blocks": 5}
        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 0

    @pytest.mark.asyncio
    async def test_counter_not_incremented_when_block_pending_false(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """First block should NOT touch the counter (tool_block_pending is false)."""

        _insert_rule(
            manager,
            "block-edit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effect=RuleEffect(type="block", reason="No editing", tools=["Edit"]),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("consecutive_tool_blocks", 0) == 0

    @pytest.mark.asyncio
    async def test_counter_not_reset_on_failed_after_tool(self, db: LocalDatabase) -> None:
        """Counter should NOT reset on failed after_tool."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "tool_block_pending": True,
            "consecutive_tool_blocks": 2,
        }
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        event.metadata["is_failure"] = True
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 2


class TestNoRules:
    @pytest.mark.asyncio
    async def test_no_matching_rules_allows(self, db: LocalDatabase) -> None:
        """When no rules match the event, the response should allow."""

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"


class TestOverrideCollectsMcpCalls:
    """Tests that mcp_call effects fire even when override decisions apply."""

    @pytest.mark.asyncio
    async def test_tool_block_pending_still_collects_mcp_calls(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When tool_block_pending blocks a stop, mcp_call effects should still be collected."""

        _insert_rule(
            manager,
            "digest-on-response",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(
                    type="mcp_call",
                    server="gobby-memory",
                    tool="build_turn_and_digest",
                    arguments={"session_id": "test"},
                    background=True,
                ),
            ),
            priority=11,
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"tool_block_pending": True}
        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "tool-failure-recovery" in (response.reason or "")
        # The critical assertion: mcp_calls must be collected despite the override block
        calls = response.metadata.get("mcp_calls", [])
        assert len(calls) == 1
        assert calls[0]["tool"] == "build_turn_and_digest"
        # tool_block_pending should still be cleared
        assert variables["tool_block_pending"] is False

    @pytest.mark.asyncio
    async def test_force_allow_stop_still_collects_mcp_calls(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When force_allow_stop allows a stop, mcp_call effects should still be collected."""

        _insert_rule(
            manager,
            "digest-on-response",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(
                    type="mcp_call",
                    server="gobby-memory",
                    tool="build_turn_and_digest",
                    arguments={"session_id": "test"},
                    background=True,
                ),
            ),
            priority=11,
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"force_allow_stop": True}
        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        calls = response.metadata.get("mcp_calls", [])
        assert len(calls) == 1
        assert calls[0]["tool"] == "build_turn_and_digest"
        assert variables["force_allow_stop"] is False

    @pytest.mark.asyncio
    async def test_override_block_trumps_rule_evaluated_allow(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending override should block even when no rules produce a block."""

        # Only a set_variable rule — no block rules
        _insert_rule(
            manager,
            "harmless-rule",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="set_variable", variable="ran", value=True),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"tool_block_pending": True}
        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert variables.get("ran") is True  # rule loop still ran

    @pytest.mark.asyncio
    async def test_force_allow_trumps_rule_evaluated_block(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """force_allow_stop override should allow even when rules produce a block."""

        _insert_rule(
            manager,
            "block-stop",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effect=RuleEffect(type="block", reason="Cannot stop yet"),
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"force_allow_stop": True}
        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"


class TestMcpCallTemplateRendering:
    """Tests for Jinja2 template rendering in mcp_call effect arguments."""

    @pytest.mark.asyncio
    async def test_mcp_call_template_rendering(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Template expressions in mcp_call arguments should be rendered."""

        _insert_rule(
            manager,
            "pipeline-auto-run",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                when="variables.get('_assigned_pipeline')",
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-workflows",
                        tool="run_pipeline",
                        arguments={"name": "{{ _assigned_pipeline }}"},
                        background=True,
                    ),
                ],
            ),
        )

        event = _make_event(HookEventType.SESSION_START)
        engine = RuleEngine(db)
        variables: dict[str, Any] = {"_assigned_pipeline": "my-deploy-pipeline"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 1
        call = mcp_calls[0]
        assert call["server"] == "gobby-workflows"
        assert call["tool"] == "run_pipeline"
        assert call["arguments"]["name"] == "my-deploy-pipeline"
        assert call["background"] is True

    @pytest.mark.asyncio
    async def test_mcp_call_static_args_passthrough(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Non-template arguments should pass through unchanged."""

        _insert_rule(
            manager,
            "static-mcp-call",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-tasks",
                        tool="list_tasks",
                        arguments={"status": "open", "limit": 10},
                    ),
                ],
            ),
        )

        event = _make_event(HookEventType.SESSION_START)
        engine = RuleEngine(db)
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 1
        call = mcp_calls[0]
        assert call["arguments"]["status"] == "open"
        assert call["arguments"]["limit"] == 10
