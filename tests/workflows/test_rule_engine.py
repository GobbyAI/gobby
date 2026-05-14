"""Tests for RuleEngine single-pass evaluation loop."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentSelector,
    AgentWorkflows,
    RuleDefinitionBody,
    RuleEffect,
    RuleEvent,
    RuleTriggerEvent,
)
from gobby.workflows.engine.core import RuleEngine

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
    tags: list[str] | None = None,
) -> str:
    """Helper to insert a rule into the database."""
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=priority,
        enabled=enabled,
        sources=sources,
        tags=tags,
    )
    return row.id


def _insert_agent(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    *,
    include: list[str],
    exclude: list[str] | None = None,
    rules: list[str] | None = None,
) -> str:
    body = AgentDefinitionBody(
        name=name,
        workflows=AgentWorkflows(
            rules=rules or [],
            rule_selectors=AgentSelector(include=include, exclude=exclude or []),
        ),
    )
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="agent",
        source="custom",
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
                effects=[RuleEffect(type="block", reason="blocked")],
            ),
        )
        _insert_rule(
            manager,
            "after-tool-rule",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[RuleEffect(type="set_variable", variable="x", value=True)],
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
                effects=[RuleEffect(type="block", reason="should not fire")],
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
                effects=[RuleEffect(type="block", reason="No editing allowed", tools=["Edit"])],
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
                effects=[RuleEffect(type="block", reason="No editing", tools=["Edit"])],
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
                effects=[RuleEffect(type="block", reason="First block")],
            ),
            priority=10,
        )
        _insert_rule(
            manager,
            "low-priority-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason="Second block")],
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
                effects=[RuleEffect(type="set_variable", variable="task_claimed", value=True)],
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
                effects=[
                    RuleEffect(
                        type="set_variable",
                        variable="custom_counter",
                        value="variables.get('custom_counter', 0) + 1",
                    )
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"custom_counter": 2}
        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["custom_counter"] == 3

    @pytest.mark.asyncio
    async def test_set_variable_jinja2_template(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """set_variable with Jinja2 template should render and coerce to native type."""

        _insert_rule(
            manager,
            "increment-via-jinja",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effects=[
                    RuleEffect(
                        type="set_variable",
                        variable="error_triage_blocks",
                        value="{{ variables.get('error_triage_blocks', 0) + 1 }}",
                    )
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"error_triage_blocks": 2}
        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["error_triage_blocks"] == 3


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
                effects=[
                    RuleEffect(
                        type="inject_context",
                        template="You are working on an important task.",
                    )
                ],
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
                effects=[RuleEffect(type="inject_context", template="Context A.")],
            ),
            priority=10,
        )
        _insert_rule(
            manager,
            "inject-b",
            RuleDefinitionBody(
                event=RuleEvent.SESSION_START,
                effects=[RuleEffect(type="inject_context", template="Context B.")],
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
                effects=[RuleEffect(type="block", reason="Use uv")],
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
                effects=[RuleEffect(type="block", reason="Use uv")],
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
                effects=[RuleEffect(type="set_variable", variable="stop_attempts", value=0)],
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
                effects=[RuleEffect(type="set_variable", variable="x", value=1)],
            ),
            priority=20,
        )
        _insert_rule(
            manager,
            "set-x-to-2",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[RuleEffect(type="set_variable", variable="x", value=2)],
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
                effects=[RuleEffect(type="block", reason="Blocked!")],
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
                effects=[RuleEffect(type="block", reason="Blocked!")],
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
                effects=[
                    RuleEffect(
                        type="observe",
                        category="tool_use",
                        message="Tool was used",
                    )
                ],
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
                effects=[RuleEffect(type="observe", category="a", message="first")],
            ),
            priority=10,
        )
        _insert_rule(
            manager,
            "observe-b",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[RuleEffect(type="observe", category="b", message="second")],
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
                effects=[RuleEffect(type="observe", message="no category")],
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
                effects=[
                    RuleEffect(
                        type="observe",
                        category="tool_use",
                        message="Used {{ event.data.get('tool_name', 'unknown') }}",
                    )
                ],
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
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-memory",
                        tool="recall_with_synthesis",
                        arguments={"limit": 5},
                    )
                ],
            ),
        )

        event = _make_event(HookEventType.BEFORE_AGENT)
        response = await _assert_evaluation(db, event, "allow", variables={"servers_listed": True})
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
                effects=[RuleEffect(type="set_variable", variable="flag", value=True)],
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
                effects=[RuleEffect(type="block", reason="Flag was set")],
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
                effects=[
                    RuleEffect(
                        type="block",
                        reason="Must provide commit_sha when closing a task",
                        tools=["call_tool"],
                    )
                ],
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
    def test_toggle_rule_works_for_installed(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """toggle_rule should work for installed rules."""
        from gobby.mcp_proxy.tools.workflows._rules import toggle_rule

        manager.create(
            name="installed-rule",
            definition_json=RuleDefinitionBody(
                event=RuleEvent.STOP,
                effects=[RuleEffect(type="block", reason="installed")],
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

    def test_validation_no_effects_error(self) -> None:
        """Omitting effects should raise ValidationError."""
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="required and must be non-empty"):
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


class TestBeforeToolBlockTracking:
    """Tests for engine tracking of blocked BEFORE_TOOL attempts."""

    @pytest.mark.asyncio
    async def test_before_tool_block_records_last_blocked_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """A before_tool block should record the blocked tool name only."""

        _insert_rule(
            manager,
            "block-edit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason="No editing", tools=["Edit"])],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("_last_blocked_tool") == "Edit"
        assert variables.get("tool_block_pending") is None

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
                effects=[RuleEffect(type="block", reason="Cannot stop yet")],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.STOP)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("tool_block_pending") is None

    @pytest.mark.asyncio
    async def test_before_tool_block_with_multi_effect_tracks_tool_name(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """A multi-effect before_tool block should still track the blocked tool."""

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
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("_last_blocked_tool") == "Edit"
        assert variables.get("tool_block_pending") is not True
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
        """Counter should increment when the same blocked tool is retried."""

        _insert_rule(
            manager,
            "block-edit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason="No editing", tools=["Edit"])],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"_last_blocked_tool": "Edit"}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 1

    @pytest.mark.asyncio
    async def test_short_circuit_fires_at_threshold(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """At the configured threshold, engine should short-circuit without evaluating rules."""

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
            "consecutive_tool_blocks": 3,
            "_last_blocked_tool": "Edit",
            "max_consecutive_blocked_tool_attempts": 5,
        }
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert response.reason is not None and "5 times consecutively" in response.reason
        assert "STOP retrying" in response.reason
        # Rule should NOT have been evaluated — no side effect
        assert variables.get("rule_ran") is None

    @pytest.mark.asyncio
    async def test_counter_resets_on_successful_after_tool(self, db: LocalDatabase) -> None:
        """Counter should reset to 0 on successful after_tool."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "consecutive_tool_blocks": 3,
            "_last_blocked_tool": "Edit",
        }
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 0
        assert variables["_last_blocked_tool"] == ""

    @pytest.mark.asyncio
    async def test_counter_resets_on_turn_start_boundary(self, db: LocalDatabase) -> None:
        """Counter should reset to 0 on the semantic turn_start boundary."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"consecutive_tool_blocks": 5}
        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 0

    @pytest.mark.asyncio
    async def test_counter_not_incremented_when_block_pending_false(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """First block should NOT touch the counter when no blocked tool is tracked."""

        _insert_rule(
            manager,
            "block-edit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason="No editing", tools=["Edit"])],
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
            "consecutive_tool_blocks": 2,
            "_last_blocked_tool": "Edit",
        }
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        event.metadata["is_failure"] = True
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["consecutive_tool_blocks"] == 2
        assert variables["_last_blocked_tool"] == "Edit"


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
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-memory",
                        tool="build_turn_and_digest",
                        arguments={"session_id": "test"},
                        background=True,
                    )
                ],
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
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-memory",
                        tool="build_turn_and_digest",
                        arguments={"session_id": "test"},
                        background=True,
                    )
                ],
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
                effects=[RuleEffect(type="set_variable", variable="ran", value=True)],
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
                effects=[RuleEffect(type="block", reason="Cannot stop yet")],
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


class TestToolBlockPendingScopeAware:
    """Tests for scope-aware tool_block_pending clearing."""

    @pytest.mark.asyncio
    async def test_tool_block_pending_clears_for_any_successful_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending SHOULD clear when any tool succeeds (scope-agnostic)."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "tool_block_pending": True,
            "_last_blocked_tool": "Write",
        }
        # A different tool (Read) succeeds — should still clear the pending flag
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["tool_block_pending"] is False
        assert variables["_last_blocked_tool"] == ""

    @pytest.mark.asyncio
    async def test_tool_block_pending_clears_for_matching_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending should clear when the same tool succeeds."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "tool_block_pending": True,
            "_last_blocked_tool": "Write",
        }
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Write"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["tool_block_pending"] is False
        assert variables["_last_blocked_tool"] == ""

    @pytest.mark.asyncio
    async def test_tool_block_pending_clears_when_no_last_blocked_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """tool_block_pending should clear when _last_blocked_tool is empty (legacy compat)."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "tool_block_pending": True,
            "_last_blocked_tool": "",
        }
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["tool_block_pending"] is False

    @pytest.mark.asyncio
    async def test_parallel_scenario_edit_fails_read_succeeds(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Parallel calls: Edit pre-tool fires, Edit fails, Read succeeds — edit_write_pending stays True."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}

        # Edit pre-tool fires → sets edit_write_pending
        before_event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(before_event, session_id="sess-1", variables=variables)
        assert variables.get("edit_write_pending")

        # Edit fails → sets tool_block_pending
        fail_event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        fail_event.metadata["is_failure"] = True
        await engine.evaluate(fail_event, session_id="sess-1", variables=variables)
        assert variables["tool_block_pending"] is True
        # edit_write_pending should NOT be cleared by a failed edit
        assert variables.get("edit_write_pending")

        # Read succeeds (sibling cancelled call) — edit_write_pending still True
        success_event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(success_event, session_id="sess-1", variables=variables)

        # edit_write_pending is the safety net — Read doesn't clear it
        assert variables.get("edit_write_pending")


class TestEditWritePending:
    """Tests for edit_write_pending lifecycle tracking."""

    @pytest.mark.asyncio
    async def test_edit_write_pending_set_on_before_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """edit_write_pending should be set True when Edit/Write pre-tool fires."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("edit_write_pending")

    @pytest.mark.asyncio
    async def test_edit_write_pending_set_for_write(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """edit_write_pending should be set for Write tool too."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("edit_write_pending")

    @pytest.mark.asyncio
    async def test_edit_write_pending_set_for_apply_patch(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Normalized apply_patch should participate in edit-write recovery."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "apply_patch",
                "tool_input": (
                    "*** Begin Patch\n*** Update File: src/main.py\n@@\n*** End Patch\n"
                ),
            },
        )
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("edit_write_pending")

    @pytest.mark.asyncio
    async def test_edit_write_pending_set_for_shell_write(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """High-confidence shell writes should participate in edit recovery."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "tool_input": {"command": "printf hi > src/main.py"}},
        )
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("edit_write_pending")

    @pytest.mark.asyncio
    async def test_edit_write_pending_not_set_for_read(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """edit_write_pending should NOT be set for non-edit tools like Read."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("edit_write_pending") is not True

    @pytest.mark.asyncio
    async def test_edit_write_pending_cleared_on_success(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """edit_write_pending should clear on successful Edit/Write post-tool."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"edit_write_pending": True}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["edit_write_pending"] is False

    @pytest.mark.asyncio
    async def test_edit_write_pending_cleared_on_apply_patch_success(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Successful normalized apply_patch should clear edit_write_pending."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"edit_write_pending": True}
        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "apply_patch",
                "tool_input": (
                    "*** Begin Patch\n*** Update File: src/main.py\n@@\n*** End Patch\n"
                ),
            },
        )
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["edit_write_pending"] is False

    @pytest.mark.asyncio
    async def test_edit_write_pending_not_cleared_on_failure(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """edit_write_pending should NOT clear on failed Edit/Write post-tool."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"edit_write_pending": True}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        event.metadata["is_failure"] = True
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["edit_write_pending"] is True

    @pytest.mark.asyncio
    async def test_edit_write_pending_blocks_stop(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Stop should be blocked when edit_write_pending is True."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"edit_write_pending": True}
        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert response.reason is not None and "edit-write-recovery" in response.reason

    @pytest.mark.asyncio
    async def test_edit_write_pending_stop_allowed_after_success(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Stop should be allowed after edit_write_pending is cleared by success."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"edit_write_pending": True}

        # Successful Edit clears the flag
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Edit"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert variables["edit_write_pending"] is False

        # Now stop should be allowed
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, session_id="sess-1", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_edit_write_pending_circuit_breaker(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Circuit breaker: after 3 stop blocks, allow stop and clear flag."""

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"edit_write_pending": True, "edit_write_stop_blocks": 0}

        # Blocks 1, 2, 3
        for i in range(3):
            event = _make_event(HookEventType.STOP)
            response = await engine.evaluate(event, session_id="sess-1", variables=variables)
            assert response.decision == "block", f"Block {i + 1} should block"

        assert variables["edit_write_stop_blocks"] == 3

        # 4th attempt — circuit breaker trips, allow stop
        event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert response.decision == "allow"
        assert variables["edit_write_pending"] is False
        assert variables["edit_write_stop_blocks"] == 0

    @pytest.mark.asyncio
    async def test_edit_write_pending_cleared_when_blocked_by_rule(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Edit blocked by a declarative rule should clear edit_write_pending.

        Regression test: when require-task-before-edit blocks an Edit,
        edit_write_pending stayed True, causing edit-write-recovery to
        loop on every stop attempt.
        """
        # Install a rule that blocks Edit on before_tool
        _insert_rule(
            manager,
            "block-edit-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                tools=["Edit"],
                effects=[RuleEffect(type="block", reason="No task claimed")],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}

        # Fire before_tool for Edit — rule blocks it
        edit_event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        response = await engine.evaluate(edit_event, session_id="sess-1", variables=variables)
        assert response.decision == "block"

        # edit_write_pending should be cleared since the edit was rule-blocked
        assert variables.get("edit_write_pending") is False

        # Stop should now be allowed — a gate-blocked edit is not a tool execution failure
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, session_id="sess-1", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_edit_write_pending_cleared_by_non_edit_success(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Any successful non-edit tool should clear a stale edit_write_pending flag.

        Regression test: edit_write_pending could stay True after Edit succeeded
        (e.g. AFTER_TOOL lost or tool_name mismatch), causing false-positive
        edit-write-recovery blocks on stop.
        """
        engine = RuleEngine(db)
        # Simulate stale flag (Edit succeeded but flag wasn't cleared)
        variables: dict[str, Any] = {"edit_write_pending": True}

        # Successful Read should clear the stale flag
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["edit_write_pending"] is False

        # Stop should be allowed
        stop_event = _make_event(HookEventType.STOP)
        response = await engine.evaluate(stop_event, session_id="sess-1", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_edit_write_pending_not_cleared_by_failed_non_edit(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Failed non-edit tool should NOT clear edit_write_pending."""
        engine = RuleEngine(db)
        variables: dict[str, Any] = {"edit_write_pending": True}

        # Failed Bash should NOT clear the flag
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Bash"})
        event.metadata["is_failure"] = True
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["edit_write_pending"] is True


class TestInlineMcpCallDispatch:
    """Tests for inline mcp_call dispatch (inject_result atomicity)."""

    @pytest.mark.asyncio
    async def test_inline_dispatch_success_formats_get_skill_as_directive(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Daemon-side get_skill inject_result should format a fetch directive."""
        _insert_rule(
            manager,
            "inject-skill",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-skills",
                        tool="get_skill",
                        arguments={"name": "python"},
                        inject_result=True,
                    ),
                    RuleEffect(
                        type="set_variable",
                        variable="injected",
                        value=True,
                    ),
                ],
            ),
        )

        async def mock_dispatcher(server: str, tool: str, args: dict, event: Any) -> dict:
            return {
                "success": True,
                "inject_result": True,
                "result": {"skill": {"name": "python", "content": "# Python skill content"}},
            }

        engine = RuleEngine(db, mcp_dispatcher=mock_dispatcher)
        variables: dict[str, Any] = {}
        event = _make_event(data={"tool_name": "Read"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.context == 'Call get_skill(name="python") on gobby-skills, then continue.'
        assert "# Python skill content" not in (response.context or "")
        # Variable should be set (inline dispatch succeeded)
        assert variables.get("injected") is True
        # Should NOT appear in deferred mcp_calls
        assert len(response.metadata.get("mcp_calls", [])) == 0

    @pytest.mark.asyncio
    async def test_inline_dispatch_failure_aborts_set_variable(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When inline dispatch fails, sibling set_variable should NOT fire."""
        _insert_rule(
            manager,
            "inject-skill-fail",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-skills",
                        tool="get_skill",
                        arguments={"name": "python"},
                        inject_result=True,
                    ),
                    RuleEffect(
                        type="set_variable",
                        variable="injected",
                        value=True,
                    ),
                ],
            ),
        )

        async def mock_dispatcher_fail(server: str, tool: str, args: dict, event: Any) -> dict:
            return {"success": False, "result": {"error": "skill not found"}}

        engine = RuleEngine(db, mcp_dispatcher=mock_dispatcher_fail)
        variables: dict[str, Any] = {}
        event = _make_event(data={"tool_name": "Read"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        # Variable should NOT be set (dispatch failed, effects aborted)
        assert variables.get("injected") is None
        # No deferred calls either
        assert len(response.metadata.get("mcp_calls", [])) == 0

    @pytest.mark.asyncio
    async def test_inline_dispatch_exception_aborts_set_variable(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When inline dispatch raises, sibling set_variable should NOT fire."""
        _insert_rule(
            manager,
            "inject-skill-exc",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-skills",
                        tool="get_skill",
                        arguments={"name": "python"},
                        inject_result=True,
                    ),
                    RuleEffect(
                        type="set_variable",
                        variable="injected",
                        value=True,
                    ),
                ],
            ),
        )

        async def mock_dispatcher_raise(server: str, tool: str, args: dict, event: Any) -> dict:
            raise RuntimeError("connection refused")

        engine = RuleEngine(db, mcp_dispatcher=mock_dispatcher_raise)
        variables: dict[str, Any] = {}
        event = _make_event(data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        # Variable should NOT be set
        assert variables.get("injected") is None

    @pytest.mark.asyncio
    async def test_non_inject_mcp_call_still_deferred(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """mcp_call without inject_result should still be deferred."""
        _insert_rule(
            manager,
            "background-call",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-memory",
                        tool="recall",
                        arguments={"limit": 5},
                    ),
                ],
            ),
        )

        call_count = 0

        async def mock_dispatcher(server: str, tool: str, args: dict, event: Any) -> dict:
            nonlocal call_count
            call_count += 1
            return {"success": True, "result": {}}

        engine = RuleEngine(db, mcp_dispatcher=mock_dispatcher)
        event = _make_event(data={"tool_name": "Read"})
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        # Should be deferred, not dispatched inline
        assert call_count == 0
        assert len(response.metadata.get("mcp_calls", [])) == 1

    @pytest.mark.asyncio
    async def test_no_dispatcher_falls_back_to_deferred(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """inject_result mcp_call without dispatcher falls back to deferred."""
        _insert_rule(
            manager,
            "inject-no-dispatcher",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-skills",
                        tool="get_skill",
                        arguments={"name": "python"},
                        inject_result=True,
                    ),
                ],
            ),
        )

        engine = RuleEngine(db, mcp_dispatcher=None)
        event = _make_event(data={"tool_name": "Read"})
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        # Should fall back to deferred
        assert len(response.metadata.get("mcp_calls", [])) == 1
        assert response.metadata["mcp_calls"][0]["inject_result"] is True

    @pytest.mark.asyncio
    async def test_background_inject_result_still_deferred(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Background mcp_call with inject_result should still be deferred."""
        _insert_rule(
            manager,
            "bg-inject",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="mcp_call",
                        server="gobby-skills",
                        tool="get_skill",
                        arguments={"name": "python"},
                        inject_result=True,
                        background=True,
                    ),
                ],
            ),
        )

        call_count = 0

        async def mock_dispatcher(server: str, tool: str, args: dict, event: Any) -> dict:
            nonlocal call_count
            call_count += 1
            return {"success": True, "result": {}}

        engine = RuleEngine(db, mcp_dispatcher=mock_dispatcher)
        event = _make_event(data={"tool_name": "Read"})
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        # Background should NOT dispatch inline
        assert call_count == 0
        assert len(response.metadata.get("mcp_calls", [])) == 1


class TestRuleEngineHelpers:
    """Tests for internal helper methods of RuleEngine."""

    def test_get_tool_identity_regular_tool(self) -> None:
        """Regular tool name is returned as-is."""
        from gobby.workflows.engine.core import _get_tool_identity

        assert _get_tool_identity({"tool_name": "Edit"}) == "Edit"
        assert _get_tool_identity({"tool_name": "Read"}) == "Read"

    def test_get_tool_identity_mcp_call_tool(self) -> None:
        """MCP call_tool returns 'server:tool' identity."""
        from gobby.workflows.engine.core import _get_tool_identity

        result = _get_tool_identity(
            {
                "tool_name": "call_tool",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            }
        )
        assert result == "gobby-tasks:list_tasks"

    def test_get_tool_identity_mcp_gobby_call_tool(self) -> None:
        """mcp__gobby__call_tool returns 'server:tool' identity."""
        from gobby.workflows.engine.core import _get_tool_identity

        result = _get_tool_identity(
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {"server_name": "gobby-memory", "tool_name": "store"},
            }
        )
        assert result == "gobby-memory:store"

    def test_get_tool_identity_mcp_missing_fields(self) -> None:
        """MCP call_tool without server/tool returns tool_name."""
        from gobby.workflows.engine.core import _get_tool_identity

        result = _get_tool_identity(
            {
                "tool_name": "call_tool",
                "tool_input": {},
            }
        )
        assert result == "call_tool"

    def test_get_tool_identity_mcp_non_dict_input(self) -> None:
        """MCP call_tool with non-dict tool_input returns tool_name."""
        from gobby.workflows.engine.core import _get_tool_identity

        result = _get_tool_identity(
            {
                "tool_name": "call_tool",
                "tool_input": "not a dict",
            }
        )
        assert result == "call_tool"

    def test_mcp_tool_matches_exact(self) -> None:
        """Exact MCP tool key match."""
        assert RuleEngine._mcp_tool_matches("gobby-tasks:list", ["gobby-tasks:list"]) is True

    def test_mcp_tool_matches_wildcard(self) -> None:
        """Wildcard 'server:*' match."""
        assert RuleEngine._mcp_tool_matches("gobby-tasks:list", ["gobby-tasks:*"]) is True

    def test_mcp_tool_no_match(self) -> None:
        """No match returns False."""
        assert RuleEngine._mcp_tool_matches("gobby-tasks:list", ["other:*"]) is False
        assert RuleEngine._mcp_tool_matches("gobby-tasks:list", ["other:list"]) is False

    def test_is_expression_true(self) -> None:
        """Expression-like strings should be detected."""
        engine = RuleEngine.__new__(RuleEngine)
        assert engine._is_expression("variables.get('x')") is True
        assert engine._is_expression("len(items)") is True
        assert engine._is_expression("a + b") is True
        assert engine._is_expression("x and y") is True
        assert engine._is_expression("x not y") is True
        assert engine._is_expression("assistant_response_matches_any(['In summary'])") is True
        assert (
            engine._is_expression(
                """
                assistant_response_matches_any([
                    'In summary',
                ])
                """
            )
            is True
        )

    def test_is_expression_false(self) -> None:
        """Literal strings should not be detected as expressions."""
        engine = RuleEngine.__new__(RuleEngine)
        assert engine._is_expression("hello world") is False
        assert engine._is_expression("42") is False
        assert engine._is_expression("true") is False

    def test_coerce_rendered_value(self) -> None:
        """Test _coerce_rendered_value for bool, int, float, string."""
        assert RuleEngine._coerce_rendered_value("true") is True
        assert RuleEngine._coerce_rendered_value("True") is True
        assert RuleEngine._coerce_rendered_value("false") is False
        assert RuleEngine._coerce_rendered_value("42") == 42
        assert RuleEngine._coerce_rendered_value("3.14") == 3.14
        assert RuleEngine._coerce_rendered_value("hello") == "hello"
        assert RuleEngine._coerce_rendered_value("  True  ") is True

    def test_render_template_no_braces(self, db: LocalDatabase) -> None:
        """Template without {{ }} is returned as-is."""
        engine = RuleEngine(db)
        result = engine._render_template("plain text", {}, {})
        assert result == "plain text"

    def test_has_pending_messages_empty_session(self, db: LocalDatabase) -> None:
        """_has_pending_messages returns False for empty session_id."""
        engine = RuleEngine(db)
        assert engine._has_pending_messages("") is False

    def test_pending_message_count_empty_session(self, db: LocalDatabase) -> None:
        """_pending_message_count returns 0 for empty session_id."""
        engine = RuleEngine(db)
        assert engine._pending_message_count("") == 0

    def test_has_pending_messages_no_messages(self, db: LocalDatabase) -> None:
        """_has_pending_messages returns False when no messages exist."""
        engine = RuleEngine(db)
        result = engine._has_pending_messages("sess-nonexistent")
        assert result is False

    def test_pending_message_count_no_messages(self, db: LocalDatabase) -> None:
        """_pending_message_count returns 0 when no messages exist."""
        engine = RuleEngine(db)
        result = engine._pending_message_count("sess-nonexistent")
        assert result == 0


class TestCatastrophicFailure:
    """Tests for catastrophic failure detection."""

    @pytest.mark.asyncio
    async def test_catastrophic_failure_sets_force_allow_stop(self, db: LocalDatabase) -> None:
        """Catastrophic failure in tool output should set force_allow_stop."""
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Bash", "tool_output": "Error: rate limit exceeded"},
        )
        event.metadata["is_failure"] = True
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("force_allow_stop") is True

    @pytest.mark.asyncio
    async def test_non_catastrophic_failure_no_force_allow(self, db: LocalDatabase) -> None:
        """Non-catastrophic failure should NOT set force_allow_stop."""
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Edit", "tool_output": "File not found"},
        )
        event.metadata["is_failure"] = True
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("force_allow_stop") is not True

    @pytest.mark.asyncio
    async def test_catastrophic_patterns(self, db: LocalDatabase) -> None:
        """All catastrophic patterns should be detected."""
        engine = RuleEngine(db)
        for pattern in ["out of usage", "quota exceeded", "billing", "account suspended"]:
            variables: dict[str, Any] = {}
            event = _make_event(
                HookEventType.AFTER_TOOL,
                data={"tool_name": "Bash", "tool_output": f"Error: {pattern}"},
            )
            event.metadata["is_failure"] = True
            await engine.evaluate(event, session_id="sess-1", variables=variables)
            assert variables.get("force_allow_stop") is True, f"Pattern '{pattern}' not detected"


class TestStopAttempts:
    """Tests for automatic stop_attempts counter."""

    @pytest.mark.asyncio
    async def test_stop_attempts_increments(self, db: LocalDatabase) -> None:
        """stop_attempts should increment on each STOP event."""
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}

        for i in range(3):
            event = _make_event(HookEventType.STOP)
            await engine.evaluate(event, session_id="sess-1", variables=variables)
            assert variables["stop_attempts"] == i + 1

    @pytest.mark.asyncio
    async def test_stop_attempts_resets_on_turn_start_boundary(self, db: LocalDatabase) -> None:
        """stop_attempts should reset on the semantic turn_start boundary."""
        engine = RuleEngine(db)
        variables: dict[str, Any] = {"stop_attempts": 5}

        event = _make_event(HookEventType.BEFORE_AGENT)
        await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert variables["stop_attempts"] == 0


class TestEnforcementToggle:
    """Tests for global enforcement toggle."""

    @pytest.mark.asyncio
    async def test_enforcement_disabled_allows_everything(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When enforcement is globally disabled, all events are allowed."""
        from gobby.storage.config_store import ConfigStore

        _insert_rule(
            manager,
            "should-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason="blocked")],
            ),
        )

        config_store = ConfigStore(db)
        config_store.set("rules.enforcement_enabled", False)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"

        # Clean up
        config_store.set("rules.enforcement_enabled", None)


class TestUnmappedEventType:
    """Tests for unmapped event types."""

    @pytest.mark.asyncio
    async def test_unmapped_event_type_allows(self, db: LocalDatabase) -> None:
        """Event types not in _EVENT_TYPE_MAP should be allowed."""
        engine = RuleEngine(db)
        # Use a custom event type that's not mapped
        event = _make_event(HookEventType.BEFORE_TOOL)
        # Manually set to an unmapped type
        event.event_type = "custom_unmapped_type"  # type: ignore[assignment]
        response = await engine.evaluate(event, session_id="sess-1", variables={})
        assert response.decision == "allow"


class TestTurnEndResolution:
    @pytest.mark.asyncio
    async def test_before_agent_resolves_to_raw_and_semantic_start(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "raw-before-agent",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_AGENT,
                effects=[RuleEffect(type="set_variable", variable="raw_start", value=True)],
            ),
        )
        _insert_rule(
            manager,
            "semantic-turn-start",
            RuleDefinitionBody(
                event=RuleTriggerEvent.TURN_START,
                effects=[RuleEffect(type="set_variable", variable="semantic_start", value=True)],
            ),
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_AGENT)
        await _assert_evaluation(db, event, "allow", variables=variables)

        assert variables["raw_start"] is True
        assert variables["semantic_start"] is True

    @pytest.mark.asyncio
    async def test_turn_start_rule_fires_for_before_agent(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "turn-start-before-agent",
            RuleDefinitionBody(
                event=RuleTriggerEvent.TURN_START,
                effects=[RuleEffect(type="set_variable", variable="matched_start", value=True)],
            ),
        )

        variables: dict[str, Any] = {"stop_attempts": 5}
        event = _make_event(HookEventType.BEFORE_AGENT)
        await _assert_evaluation(db, event, "allow", variables=variables)

        assert variables["matched_start"] is True
        assert variables["stop_attempts"] == 0

    @pytest.mark.asyncio
    async def test_turn_end_rule_fires_for_stop(self, db: LocalDatabase, manager) -> None:
        _insert_rule(
            manager,
            "turn-end-stop",
            RuleDefinitionBody(
                event=RuleTriggerEvent.TURN_END,
                effects=[RuleEffect(type="set_variable", variable="matched", value=True)],
            ),
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.STOP)
        await _assert_evaluation(db, event, "allow", variables=variables)

        assert variables["matched"] is True
        assert variables["stop_attempts"] == 1

    @pytest.mark.asyncio
    async def test_turn_end_rule_fires_for_after_agent(self, db: LocalDatabase, manager) -> None:
        _insert_rule(
            manager,
            "turn-end-after-agent",
            RuleDefinitionBody(
                event=RuleTriggerEvent.TURN_END,
                effects=[RuleEffect(type="set_variable", variable="matched", value=True)],
            ),
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_AGENT, source=SessionSource.GEMINI)
        await _assert_evaluation(db, event, "allow", variables=variables)

        assert variables["matched"] is True
        assert variables["stop_attempts"] == 1

    @pytest.mark.asyncio
    async def test_raw_stop_rule_does_not_fire_for_after_agent(
        self, db: LocalDatabase, manager
    ) -> None:
        _insert_rule(
            manager,
            "raw-stop-only",
            RuleDefinitionBody(
                event=RuleEvent.STOP,
                effects=[RuleEffect(type="set_variable", variable="raw_stop", value=True)],
            ),
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_AGENT, source=SessionSource.CODEX)
        await _assert_evaluation(db, event, "allow", variables=variables)

        assert variables.get("raw_stop") is not True

    @pytest.mark.asyncio
    async def test_raw_after_agent_rule_does_not_fire_for_stop(
        self, db: LocalDatabase, manager
    ) -> None:
        _insert_rule(
            manager,
            "raw-after-agent-only",
            RuleDefinitionBody(
                event=RuleTriggerEvent.AFTER_AGENT,
                effects=[RuleEffect(type="set_variable", variable="raw_after_agent", value=True)],
            ),
        )

        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.STOP)
        await _assert_evaluation(db, event, "allow", variables=variables)

        assert variables.get("raw_after_agent") is not True


class TestLiveActiveRuleSelection:
    """Rules should be selected from the current agent definition at evaluation time."""

    @pytest.mark.asyncio
    async def test_stale_active_rule_names_do_not_hide_new_default_rule(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Resolve default rules from live selectors even when session names are stale."""
        _insert_agent(manager, "default", include=["tag:default"])
        _insert_rule(
            manager,
            "new-default-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="set_variable", variable="matched", value=True)],
            ),
            tags=["default"],
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "_agent_type": "default",
            "_active_rule_names": ["old-default-rule"],
        }
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})

        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("matched") is True

    @pytest.mark.asyncio
    async def test_non_default_agent_excludes_rules_outside_live_selectors(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Exclude default-only rules when the current live agent selector differs."""
        _insert_agent(manager, "backend-developer", include=["tag:worker-safety"])
        _insert_rule(
            manager,
            "default-only-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="set_variable", variable="default_matched", value=True)],
            ),
            tags=["default"],
        )
        _insert_rule(
            manager,
            "worker-safety-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="set_variable", variable="worker_matched", value=True)],
            ),
            tags=["worker-safety"],
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "_agent_type": "backend-developer",
            "_active_rule_names": ["default-only-rule", "worker-safety-rule"],
        }
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})

        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("worker_matched") is True
        assert variables.get("default_matched") is None

    @pytest.mark.asyncio
    async def test_active_rule_names_remain_fallback_when_agent_cannot_resolve(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Fall back to stored active rule names when the live agent is missing."""
        _insert_rule(
            manager,
            "allowed-by-fallback",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="set_variable", variable="allowed", value=True)],
            ),
        )
        _insert_rule(
            manager,
            "blocked-by-fallback",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="set_variable", variable="blocked", value=True)],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "_agent_type": "missing-agent",
            "_active_rule_names": ["allowed-by-fallback"],
        }
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})

        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("allowed") is True
        assert variables.get("blocked") is None


class TestAgentScope:
    """Tests for agent_scope filtering."""

    @pytest.mark.asyncio
    async def test_agent_scope_wildcard_matches_any(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with agent_scope=['*'] should match any agent type."""
        _insert_rule(
            manager,
            "wildcard-scope",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["*"],
                effects=[RuleEffect(type="set_variable", variable="matched", value=True)],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {"_agent_type": "researcher"}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert variables.get("matched") is True

    @pytest.mark.asyncio
    async def test_agent_scope_no_agent_type_excludes(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with agent_scope should be excluded when no _agent_type is set."""
        _insert_rule(
            manager,
            "scoped-rule",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["researcher"],
                effects=[RuleEffect(type="set_variable", variable="matched", value=True)],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert variables.get("matched") is None


class TestObserveEffectExtended:
    """Tests for observe effect type."""

    @pytest.mark.asyncio
    async def test_observe_effect_records_observation(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """observe effect should record an observation in variables."""
        _insert_rule(
            manager,
            "observer-rule",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[
                    RuleEffect(
                        type="observe",
                        message="Tool completed",
                        category="tool_usage",
                    ),
                ],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Read"})
        await engine.evaluate(event, session_id="sess-1", variables=variables)

        obs = variables.get("_observations", [])
        assert len(obs) == 1
        assert obs[0]["category"] == "tool_usage"
        assert obs[0]["message"] == "Tool completed"
        assert obs[0]["rule"] == "observer-rule"


class TestConsecutiveBlockDifferentTool:
    """Tests for consecutive block counter reset on different tool."""

    @pytest.mark.asyncio
    async def test_different_tool_resets_counter(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Using a different tool should reset consecutive_tool_blocks counter."""
        _insert_rule(
            manager,
            "block-all",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason="All blocked")],
            ),
        )

        engine = RuleEngine(db)
        variables: dict[str, Any] = {
            "consecutive_tool_blocks": 1,
            "_last_blocked_tool": "Edit",
        }

        # Try a different tool — counter should reset to 0
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        # Counter was reset because the tool changed
        assert variables["consecutive_tool_blocks"] == 0
        # But the tool is still blocked by the rule
        assert response.decision == "block"


class TestSessionOverridesExtended:
    """Tests for session-scoped rule overrides."""

    @pytest.mark.asyncio
    async def test_override_disables_rule(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Session override can disable a specific rule."""
        _insert_rule(
            manager,
            "block-everything",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason="blocked")],
            ),
        )

        # Add session override to disable this rule
        db.execute(
            "INSERT INTO rule_overrides (id, session_id, rule_name, enabled) VALUES (?, ?, ?, ?)",
            ("override-1", "sess-override", "block-everything", False),
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        response = await engine.evaluate(event, session_id="sess-override", variables={})
        assert response.decision == "allow"


class _FakeSkill:
    """Minimal skill stub for load_skill tests."""

    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.content = content


class _FakeSkillManager:
    """Minimal skill manager stub for load_skill tests."""

    def __init__(self, skills: dict[str, _FakeSkill] | None = None) -> None:
        self._skills = skills or {}

    def resolve_skill_name(self, name: str) -> _FakeSkill | None:
        return self._skills.get(name)


class TestLoadSkillEffect:
    """Tests for load_skill effect type."""

    @pytest.mark.asyncio
    async def test_load_skill_emits_fetch_directive(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """load_skill should emit a get_skill directive without skill content."""
        _insert_rule(
            manager,
            "load-plan-skill",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[RuleEffect(type="load_skill", skill="plan")],
            ),
        )

        skill_mgr = _FakeSkillManager({"plan": _FakeSkill("plan", "You are now in plan mode.")})
        engine = RuleEngine(db, skill_manager=skill_mgr)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "EnterPlanMode"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert response.context == 'Call get_skill(name="plan") on gobby-skills, then continue.'
        assert "You are now in plan mode." not in (response.context or "")

    @pytest.mark.asyncio
    async def test_load_skill_missing_skill_still_emits_directive(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """load_skill does not resolve skills daemon-side."""
        _insert_rule(
            manager,
            "load-missing-skill",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[RuleEffect(type="load_skill", skill="nonexistent")],
            ),
        )

        skill_mgr = _FakeSkillManager()
        engine = RuleEngine(db, skill_manager=skill_mgr)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Bash"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert (
            response.context == 'Call get_skill(name="nonexistent") on gobby-skills, then continue.'
        )

    @pytest.mark.asyncio
    async def test_load_skill_no_skill_manager_still_emits_directive(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """load_skill without skill_manager should still direct the agent."""
        _insert_rule(
            manager,
            "load-no-manager",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[RuleEffect(type="load_skill", skill="plan")],
            ),
        )

        engine = RuleEngine(db)  # No skill_manager
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Bash"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert response.context == 'Call get_skill(name="plan") on gobby-skills, then continue.'

    @pytest.mark.asyncio
    async def test_load_skill_with_per_effect_when(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """load_skill with per-effect when=false should be skipped."""
        _insert_rule(
            manager,
            "load-conditional",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                effects=[
                    RuleEffect(
                        type="load_skill",
                        skill="plan",
                        when="False",
                    ),
                ],
            ),
        )

        skill_mgr = _FakeSkillManager({"plan": _FakeSkill("plan", "Plan content")})
        engine = RuleEngine(db, skill_manager=skill_mgr)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "Bash"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert not (response.context or "")

    @pytest.mark.asyncio
    async def test_load_skill_with_set_variable(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """load_skill alongside set_variable in multi-effect rule."""
        _insert_rule(
            manager,
            "plan-mode-entry",
            RuleDefinitionBody(
                event=RuleEvent.AFTER_TOOL,
                when="event.data.get('tool_name') == 'EnterPlanMode'",
                effects=[
                    RuleEffect(type="set_variable", variable="plan_mode", value=True),
                    RuleEffect(type="load_skill", skill="plan"),
                ],
            ),
        )

        skill_mgr = _FakeSkillManager({"plan": _FakeSkill("plan", "Plan skill content")})
        engine = RuleEngine(db, skill_manager=skill_mgr)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.AFTER_TOOL, data={"tool_name": "EnterPlanMode"})
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert variables["plan_mode"] is True
        assert response.context == 'Call get_skill(name="plan") on gobby-skills, then continue.'
        assert "Plan skill content" not in (response.context or "")


class TestVerboseOnceBlockReason:
    """Repeat blocks of the same rule within a turn collapse to a one-liner."""

    _TERSE_HINT = "(full reason shown earlier this turn — scroll up)."

    @staticmethod
    def _full_reason() -> str:
        return (
            "This project is indexed with gcode. Use gcode for code navigation:\n"
            "- gcode outline path/to/file — symbol map (much cheaper than Read)\n"
            "- gcode search 'query' — find symbols by name\n"
            "- gcode symbol <full-uuid> — fetch exact source from a search/outline UUID"
        )

    @pytest.mark.asyncio
    async def test_first_block_emits_full_reason(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "require-code-index-skill",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason=self._full_reason())],
            ),
        )
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})

        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "gcode outline" in (response.reason or "")
        assert self._TERSE_HINT not in (response.reason or "")
        assert variables["_block_reasons_shown"] == ["require-code-index-skill"]

    @pytest.mark.asyncio
    async def test_second_block_same_rule_collapses_to_terse(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "require-code-index-skill",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason=self._full_reason())],
            ),
        )
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})

        await engine.evaluate(event, session_id="sess-1", variables=variables)
        second = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert second.decision == "block"
        assert second.reason == (
            f"Rule enforced by Gobby: [require-code-index-skill] {self._TERSE_HINT}"
        )

    @pytest.mark.asyncio
    async def test_different_rule_still_emits_full_reason(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "require-code-index-skill",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="event.data.get('tool_name') == 'Read'",
                effects=[RuleEffect(type="block", reason=self._full_reason())],
            ),
        )
        _insert_rule(
            manager,
            "require-uv",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                when="event.data.get('tool_name') == 'Bash'",
                effects=[
                    RuleEffect(
                        type="block",
                        reason="Bare python/pip is not permitted in this repo. Use uv instead.",
                    )
                ],
            ),
        )
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}

        first = await engine.evaluate(
            _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"}),
            session_id="sess-1",
            variables=variables,
        )
        second = await engine.evaluate(
            _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Bash"}),
            session_id="sess-1",
            variables=variables,
        )

        assert first.decision == "block"
        assert "gcode outline" in (first.reason or "")
        assert self._TERSE_HINT not in (first.reason or "")
        assert second.decision == "block"
        assert "Use uv instead" in (second.reason or "")
        assert self._TERSE_HINT not in (second.reason or "")
        assert sorted(variables["_block_reasons_shown"]) == [
            "require-code-index-skill",
            "require-uv",
        ]

    @pytest.mark.asyncio
    async def test_turn_start_clears_shown_set(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "require-code-index-skill",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", reason=self._full_reason())],
            ),
        )
        engine = RuleEngine(db)
        variables: dict[str, Any] = {}
        block_event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})

        await engine.evaluate(block_event, session_id="sess-1", variables=variables)
        await engine.evaluate(block_event, session_id="sess-1", variables=variables)
        assert "require-code-index-skill" in variables["_block_reasons_shown"]

        # New turn: BEFORE_AGENT is the TURN_START transport event.
        await engine.evaluate(
            _make_event(HookEventType.BEFORE_AGENT),
            session_id="sess-1",
            variables=variables,
        )
        assert variables["_block_reasons_shown"] == []

        # Next block in the new turn re-teaches with the full reason.
        third = await engine.evaluate(block_event, session_id="sess-1", variables=variables)
        assert third.decision == "block"
        assert "gcode outline" in (third.reason or "")
        assert self._TERSE_HINT not in (third.reason or "")
