"""Tests for rule trigger, effect, and definition models."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit


# --- RuleTriggerEvent tests ---


class TestRuleTriggerEvent:
    def test_enum_exposes_raw_hook_events_and_semantic_turn_boundaries(self) -> None:
        from gobby.workflows.definitions import RuleTriggerEvent

        assert len(RuleTriggerEvent) == 31

    def test_enum_values(self) -> None:
        from gobby.workflows.definitions import RuleTriggerEvent

        assert RuleTriggerEvent.TURN_START == "turn_start"
        assert RuleTriggerEvent.TURN_END == "turn_end"
        assert RuleTriggerEvent.BEFORE_TOOL == "before_tool"
        assert RuleTriggerEvent.AFTER_TOOL == "after_tool"
        assert RuleTriggerEvent.BEFORE_AGENT == "before_agent"
        assert RuleTriggerEvent.AFTER_AGENT == "after_agent"
        assert RuleTriggerEvent.SESSION_START == "session_start"
        assert RuleTriggerEvent.SESSION_END == "session_end"
        assert RuleTriggerEvent.STOP == "stop"
        assert RuleTriggerEvent.PRE_COMPACT == "pre_compact"
        assert RuleTriggerEvent.BEFORE_TOOL_SELECTION == "before_tool_selection"
        assert RuleTriggerEvent.BEFORE_MODEL == "before_model"
        assert RuleTriggerEvent.AFTER_MODEL == "after_model"
        assert RuleTriggerEvent.SUBAGENT_START == "subagent_start"
        assert RuleTriggerEvent.SUBAGENT_STOP == "subagent_stop"
        assert RuleTriggerEvent.PERMISSION_REQUEST == "permission_request"
        assert RuleTriggerEvent.PERMISSION_DENIED == "permission_denied"
        assert RuleTriggerEvent.NOTIFICATION == "notification"
        assert RuleTriggerEvent.STOP_FAILURE == "stop_failure"
        assert RuleTriggerEvent.TASK_CREATED == "task_created"
        assert RuleTriggerEvent.TASK_COMPLETED == "task_completed"
        assert RuleTriggerEvent.TEAMMATE_IDLE == "teammate_idle"
        assert RuleTriggerEvent.INSTRUCTIONS_LOADED == "instructions_loaded"
        assert RuleTriggerEvent.CONFIG_CHANGE == "config_change"
        assert RuleTriggerEvent.CWD_CHANGED == "cwd_changed"
        assert RuleTriggerEvent.FILE_CHANGED == "file_changed"
        assert RuleTriggerEvent.WORKTREE_CREATE == "worktree_create"
        assert RuleTriggerEvent.WORKTREE_REMOVE == "worktree_remove"
        assert RuleTriggerEvent.ELICITATION == "elicitation"
        assert RuleTriggerEvent.ELICITATION_RESULT == "elicitation_result"

    def test_enum_is_str(self) -> None:
        """RuleTriggerEvent should be a str enum for JSON serialization."""
        from gobby.workflows.definitions import RuleTriggerEvent

        assert isinstance(RuleTriggerEvent.BEFORE_TOOL, str)
        assert RuleTriggerEvent.BEFORE_TOOL == "before_tool"

    def test_enum_from_string(self) -> None:
        from gobby.workflows.definitions import RuleTriggerEvent

        assert RuleTriggerEvent("before_tool") == RuleTriggerEvent.BEFORE_TOOL
        assert RuleTriggerEvent("stop") == RuleTriggerEvent.STOP
        assert RuleTriggerEvent("turn_start") == RuleTriggerEvent.TURN_START
        assert RuleTriggerEvent("turn_end") == RuleTriggerEvent.TURN_END

    def test_enum_invalid_value(self) -> None:
        from gobby.workflows.definitions import RuleTriggerEvent

        with pytest.raises(ValueError):
            RuleTriggerEvent("invalid_event")


# --- RuleEffect tests ---


class TestRuleEffect:
    def test_block_effect(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="block",
            reason="Claim a task first",
            tools=["Edit", "Write"],
        )
        assert effect.type == "block"
        assert effect.reason == "Claim a task first"
        assert effect.tools == ["Edit", "Write"]

    def test_block_effect_with_mcp_tools(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="block",
            reason="Commit first",
            mcp_tools=["gobby-tasks:close_task"],
        )
        assert effect.mcp_tools == ["gobby-tasks:close_task"]

    def test_block_effect_with_command_patterns(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="block",
            reason="Use uv run instead",
            tools=["Bash"],
            command_pattern=r"(?:^|[;&|])\s*python\b",
            command_not_pattern=r"uv\s+run",
        )
        assert effect.command_pattern == r"(?:^|[;&|])\s*python\b"
        assert effect.command_not_pattern == r"uv\s+run"

    def test_set_variable_effect(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="set_variable",
            variable="task_claimed",
            value=True,
        )
        assert effect.type == "set_variable"
        assert effect.variable == "task_claimed"
        assert effect.value is True

    def test_set_variable_with_expression(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="set_variable",
            variable="stop_attempts",
            value="variables.get('stop_attempts', 0) + 1",
        )
        assert effect.variable == "stop_attempts"
        assert effect.value == "variables.get('stop_attempts', 0) + 1"

    def test_inject_context_effect(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="inject_context",
            template="## Task Context\nYou are working on {{ task_ref }}.",
        )
        assert effect.type == "inject_context"
        assert "{{ task_ref }}" in effect.template

    def test_mcp_call_effect(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="mcp_call",
            server="gobby-memory",
            tool="recall_with_synthesis",
            arguments={"limit": 5},
        )
        assert effect.type == "mcp_call"
        assert effect.server == "gobby-memory"
        assert effect.tool == "recall_with_synthesis"
        assert effect.arguments == {"limit": 5}

    def test_mcp_call_background(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="mcp_call",
            server="gobby-memory",
            tool="background_digest_and_synthesize",
            arguments={"limit": 20},
            background=True,
        )
        assert effect.background is True

    def test_mcp_call_background_defaults_false(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="mcp_call",
            server="gobby-memory",
            tool="sync_import",
        )
        assert effect.background is False

    def test_mcp_call_inject_result(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="mcp_call",
            server="_proxy",
            tool="list_mcp_servers",
            inject_result=True,
        )
        assert effect.inject_result is True
        assert effect.block_on_failure is False

    def test_mcp_call_block_on_failure(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="mcp_call",
            server="_proxy",
            tool="list_tools",
            arguments={"server_name": "gobby-tasks"},
            inject_result=True,
            block_on_failure=True,
        )
        assert effect.block_on_failure is True
        assert effect.inject_result is True

    def test_mcp_call_block_on_success(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="mcp_call",
            server="gobby-code",
            tool="search_content",
            arguments={"query": "TaskValidator"},
            inject_result=True,
            block_on_success=True,
        )
        assert effect.block_on_success is True
        assert effect.inject_result is True
        assert effect.block_on_failure is False

    def test_mcp_call_inject_result_defaults_false(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(
            type="mcp_call",
            server="gobby-memory",
            tool="sync_import",
        )
        assert effect.inject_result is False
        assert effect.block_on_failure is False
        assert effect.block_on_success is False

    def test_invalid_type_rejected(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        with pytest.raises(ValidationError):
            RuleEffect(type="invalid_type")

    def test_four_valid_types(self) -> None:
        """All four effect types should be accepted."""
        from gobby.workflows.definitions import RuleEffect

        for effect_type in ("block", "set_variable", "inject_context", "mcp_call"):
            effect = RuleEffect(type=effect_type)
            assert effect.type == effect_type

    def test_defaults_are_none(self) -> None:
        from gobby.workflows.definitions import RuleEffect

        effect = RuleEffect(type="block")
        assert effect.reason is None
        assert effect.tools is None
        assert effect.mcp_tools is None
        assert effect.command_pattern is None
        assert effect.command_not_pattern is None
        assert effect.variable is None
        assert effect.value is None
        assert effect.template is None
        assert effect.server is None
        assert effect.tool is None
        assert effect.arguments is None
        assert effect.background is False


# --- RuleDefinitionBody tests ---


class TestRuleDefinitionBody:
    def test_minimal_block_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="Not allowed", tools=["Edit"])],
        )
        assert body.event == RuleEvent.BEFORE_TOOL
        assert body.effects[0].type == "block"
        assert body.when is None
        assert body.match is None
        assert body.group is None

    def test_full_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            when="variables.get('require_task_before_edit') and not task_claimed",
            match={"tool": "Edit"},
            effects=[
                RuleEffect(
                    type="block",
                    reason="Claim a task first",
                    tools=["Edit", "Write", "NotebookEdit"],
                )
            ],
            group="task-enforcement",
        )
        assert body.event == RuleEvent.BEFORE_TOOL
        assert body.when is not None
        assert body.match == {"tool": "Edit"}
        assert body.effects[0].reason == "Claim a task first"
        assert body.group == "task-enforcement"

    def test_set_variable_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.AFTER_TOOL,
            when="event.data.get('mcp_tool') == 'claim_task'",
            effects=[RuleEffect(type="set_variable", variable="task_claimed", value=True)],
            group="task-enforcement",
        )
        assert body.event == RuleEvent.AFTER_TOOL
        assert body.effects[0].variable == "task_claimed"

    def test_inject_context_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.SESSION_START,
            when="variables.get('session_task')",
            effects=[
                RuleEffect(
                    type="inject_context",
                    template="You are working on task {{ variables.session_task }}.",
                )
            ],
            group="auto-task",
        )
        assert body.event == RuleEvent.SESSION_START
        assert body.effects[0].template is not None

    def test_mcp_call_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.SESSION_START,
            effects=[
                RuleEffect(
                    type="mcp_call",
                    server="gobby-memory",
                    tool="sync_import",
                )
            ],
            group="memory-lifecycle",
        )
        assert body.event == RuleEvent.SESSION_START
        assert body.effects[0].server == "gobby-memory"

    def test_stop_event_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.STOP,
            when="variables.get('_tool_block_pending')",
            effects=[
                RuleEffect(
                    type="block",
                    reason="A tool was blocked - follow the instructions.",
                )
            ],
            group="stop-gates",
        )
        assert body.event == RuleEvent.STOP

    def test_pre_compact_event_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.PRE_COMPACT,
            effects=[
                RuleEffect(
                    type="mcp_call",
                    server="gobby-sessions",
                    tool="set_handoff_context",
                )
            ],
            group="context-handoff",
        )
        assert body.event == RuleEvent.PRE_COMPACT

    def test_event_from_string(self) -> None:
        """RuleDefinitionBody should accept event as string."""
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event="before_tool",
            effects=[RuleEffect(type="block", reason="test")],
        )
        assert body.event == RuleEvent.BEFORE_TOOL

    def test_serialization_roundtrip(self) -> None:
        """RuleDefinitionBody should serialize to/from JSON (for definition_json storage)."""
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            when="not task_claimed",
            match={"tool": "Edit"},
            effects=[
                RuleEffect(
                    type="block",
                    reason="Claim a task",
                    tools=["Edit", "Write"],
                )
            ],
            group="task-enforcement",
        )

        # Serialize to dict/JSON
        data = body.model_dump()
        json_str = json.dumps(data)

        # Deserialize back
        restored = RuleDefinitionBody.model_validate_json(json_str)

        assert restored.event == body.event
        assert restored.when == body.when
        assert restored.match == body.match
        assert restored.effects[0].type == body.effects[0].type
        assert restored.effects[0].reason == body.effects[0].reason
        assert restored.effects[0].tools == body.effects[0].tools
        assert restored.group == body.group

    def test_model_dump_mode_json(self) -> None:
        """model_dump(mode='json') should produce JSON-serializable output."""
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.STOP,
            effects=[RuleEffect(type="set_variable", variable="stop_attempts", value=0)],
        )
        data = body.model_dump(mode="json")

        # Event should serialize as string value
        assert data["event"] == "stop"
        assert data["effects"][0]["type"] == "set_variable"
        assert data["effects"][0]["variable"] == "stop_attempts"
        assert data["effects"][0]["value"] == 0

    def test_turn_end_event_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent

        body = RuleDefinitionBody(
            event=RuleTriggerEvent.TURN_END,
            effects=[RuleEffect(type="inject_context", template="Continue working.")],
        )

        assert body.event == RuleTriggerEvent.TURN_END

    def test_turn_start_event_rule(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent

        body = RuleDefinitionBody(
            event=RuleTriggerEvent.TURN_START,
            effects=[RuleEffect(type="inject_context", template="New turn.")],
        )

        assert body.event == RuleTriggerEvent.TURN_START

    def test_required_fields(self) -> None:
        """event and effects are required."""
        from gobby.workflows.definitions import RuleDefinitionBody

        with pytest.raises(ValidationError):
            RuleDefinitionBody()

    def test_event_required(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect

        with pytest.raises(ValidationError):
            RuleDefinitionBody(effects=[RuleEffect(type="block")])

    def test_effects_required(self) -> None:
        from gobby.workflows.definitions import RuleDefinitionBody, RuleEvent

        with pytest.raises(ValidationError):
            RuleDefinitionBody(event=RuleEvent.BEFORE_TOOL)
