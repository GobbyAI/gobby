from __future__ import annotations

import pytest

from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    canonicalize_call_tool_wrapper,
)

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
