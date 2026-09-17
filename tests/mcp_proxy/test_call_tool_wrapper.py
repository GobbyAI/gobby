from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    canonical_call_tool_input,
    canonicalize_call_tool_wrapper,
)
from gobby.mcp_proxy.server import GobbyDaemonTools

pytestmark = pytest.mark.unit


def test_nested_wrapper_payload_is_canonicalized_to_target_arguments() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name=None,
        tool_name=None,
        arguments={
            "server_name": "gobby-agents",
            "tool_name": "end_agent_run",
            "arguments": {},
        },
    )

    assert canonical.server_name == "gobby-agents"
    assert canonical.tool_name == "end_agent_run"
    assert canonical.arguments == {}


def test_wrapper_intent_is_separate_from_target_intent() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name="example",
        tool_name="summarize",
        arguments={"intent": "target-value"},
        intent="wrapper-summary",
    )

    assert canonical.intent == "wrapper-summary"
    assert canonical.arguments == {"intent": "target-value"}


def test_nested_wrapper_intent_is_hoisted_without_touching_target_intent() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name=None,
        tool_name=None,
        arguments={
            "server_name": "example",
            "tool_name": "summarize",
            "intent": "wrapper-summary",
            "arguments": {"intent": "target-value"},
        },
    )

    assert canonical.intent == "wrapper-summary"
    assert canonical.arguments == {"intent": "target-value"}


def test_nested_wrapper_payload_accepts_args_alias() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name=None,
        tool_name=None,
        args={
            "server_name": "gobby-tasks",
            "tool_name": "create_task",
            "args": {"title": "Fix bug", "category": "code"},
        },
    )

    assert canonical.server_name == "gobby-tasks"
    assert canonical.tool_name == "create_task"
    assert canonical.arguments == {"title": "Fix bug", "category": "code"}


def test_top_level_route_preserves_target_argument_named_arguments() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name="example",
        tool_name="echo",
        arguments={"arguments": {"value": "target-owned"}},
    )

    assert canonical.server_name == "example"
    assert canonical.tool_name == "echo"
    assert canonical.arguments == {"arguments": {"value": "target-owned"}}


def test_top_level_route_preserves_target_project_id() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name="gobby-sessions",
        tool_name="session_stats",
        arguments={"project_id": "target-project"},
    )

    assert canonical.project_id is None
    assert canonical.arguments == {"project_id": "target-project"}


def test_top_level_project_id_stays_wrapper_context() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name="gobby-sessions",
        tool_name="session_stats",
        arguments={"project_id": "target-project"},
        project_id="wrapper-project",
    )

    assert canonical.project_id == "wrapper-project"
    assert canonical.arguments == {"project_id": "target-project"}


def test_top_level_route_preserves_target_route_like_arguments() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name="outer-server",
        tool_name="outer-tool",
        arguments={
            "server_name": "inner-server",
            "tool_name": "inner-tool",
            "session_id": "inner-session",
            "project_id": "inner-project",
            "value": "ok",
        },
        session_id="outer-session",
        project_id="outer-project",
    )

    assert canonical.server_name == "outer-server"
    assert canonical.tool_name == "outer-tool"
    assert canonical.session_id == "outer-session"
    assert canonical.project_id == "outer-project"
    assert canonical.arguments == {
        "server_name": "inner-server",
        "tool_name": "inner-tool",
        "session_id": "inner-session",
        "project_id": "inner-project",
        "value": "ok",
    }


def test_nested_wrapper_preserves_nested_target_project_id() -> None:
    canonical = canonicalize_call_tool_wrapper(
        server_name=None,
        tool_name=None,
        arguments={
            "server_name": "gobby-sessions",
            "tool_name": "session_stats",
            "project_id": "wrapper-project",
            "arguments": {"project_id": "target-project"},
        },
    )

    assert canonical.server_name == "gobby-sessions"
    assert canonical.tool_name == "session_stats"
    assert canonical.project_id == "wrapper-project"
    assert canonical.arguments == {"project_id": "target-project"}


def test_invalid_wrapper_json_still_raises_without_top_level_route() -> None:
    with pytest.raises(CallToolWrapperInputError):
        canonicalize_call_tool_wrapper(
            server_name=None,
            tool_name=None,
            arguments="{not-json",
        )


def test_canonical_call_tool_input_hoists_route_and_keeps_hook_fields() -> None:
    canonical = canonical_call_tool_input(
        {
            "args": '{"server_name": "gobby-tasks", "tool_name": "add_label", '
            '"intent": "label it", "args": {"task_id": "#1"}}',
            "session_id": "#7",
            "preflight_enabled": True,
        }
    )

    assert canonical == {
        "server_name": "gobby-tasks",
        "tool_name": "add_label",
        "arguments": {"task_id": "#1"},
        "intent": "label it",
        "session_id": "#7",
        "preflight_enabled": True,
    }


@pytest.mark.parametrize(
    "tool_input",
    [
        {"args": "{not-json", "preflight_enabled": True},
        {"server_name": "gobby-tasks", "tool_name": "add_label", "arguments": ["task"]},
    ],
    ids=["unroutable-invalid-json", "non-object-arguments"],
)
def test_canonical_call_tool_input_leaves_undispatchable_input_unchanged(
    tool_input: dict[str, object],
) -> None:
    canonical = canonical_call_tool_input(tool_input)

    assert canonical == tool_input
    assert canonical is not tool_input


class _SchemaErrorProxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, object, bool, str | None]] = []

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: object,
        session_id: object,
        *,
        enforce_workflow: bool = False,
        wrapper_originated: bool = False,
        intent: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            (server_name, tool_name, arguments, session_id, wrapper_originated, intent)
        )
        return {
            "error": "Unexpected argument 'arguments'",
            "hint": "Call end_agent_run with the target parameters directly.",
            "schema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["success", "failed", "cancelled", "timeout", "error"],
                    }
                },
                "required": ["status"],
            },
        }


@pytest.mark.asyncio
async def test_bad_end_agent_run_wrapper_call_returns_schema_help() -> None:
    proxy = _SchemaErrorProxy()
    tools = object.__new__(GobbyDaemonTools)
    tools.tool_proxy = proxy
    tools._session_manager = None
    tools._mcp_manager = MagicMock(project_id=None)

    result = await GobbyDaemonTools.call_tool(
        tools,
        server_name="gobby-agents",
        tool_name="end_agent_run",
        arguments={"arguments": {"status": "success"}},
    )

    assert result.is_error is True
    text = result.content[0].text
    assert "Unexpected argument 'arguments'" in text
    assert "Call end_agent_run with the target parameters directly." in text
    assert "Correct schema" in text
    assert '"status"' in text
    assert proxy.calls == [
        (
            "gobby-agents",
            "end_agent_run",
            {"arguments": {"status": "success"}},
            None,
            True,
            None,
        )
    ]
