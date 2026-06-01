from __future__ import annotations

import pytest

from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
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


def test_invalid_wrapper_json_still_raises_without_top_level_route() -> None:
    with pytest.raises(CallToolWrapperInputError):
        canonicalize_call_tool_wrapper(
            server_name=None,
            tool_name=None,
            arguments="{not-json",
        )


class _SchemaErrorProxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object, object]] = []

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: object,
        session_id: object,
    ) -> dict[str, object]:
        self.calls.append((server_name, tool_name, arguments, session_id))
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

    result = await GobbyDaemonTools.call_tool(
        tools,
        server_name="gobby-agents",
        tool_name="end_agent_run",
        arguments={"arguments": {"status": "success"}},
    )

    assert result.isError is True
    text = result.content[0].text
    assert "Unexpected argument 'arguments'" in text
    assert "Call end_agent_run with the target parameters directly." in text
    assert "Correct schema" in text
    assert '"status"' in text
    assert proxy.calls == [
        ("gobby-agents", "end_agent_run", {"arguments": {"status": "success"}}, None)
    ]
