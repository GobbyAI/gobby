"""The observer credits each identity a batch page completed, even mid-batch."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.observer_mcp import detect_mcp_call

pytestmark = pytest.mark.unit

SESSION_ID = "33333333-3333-4333-8333-333333333333"


def _after_get_skill(output: dict[str, Any]) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-skills",
            "tool_name": "get_skill",
            "arguments": {"names": ["brevity", "gobby:references/a.md", "restraint"]},
        },
        "tool_output": output,
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        source=SessionSource.CLAUDE,
        session_id=SESSION_ID,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": SESSION_ID},
    )


def _item(identity: str, text: str, *, final: bool = True) -> dict[str, str]:
    marker = f"<!-- end: {identity} -->\n" if final else ""
    return {"identity": identity, "content": f"{text}\n{marker}"}


def _page(
    instructions: list[dict[str, str]], completed: list[str], cursor: str | None
) -> dict[str, Any]:
    return {
        "success": True,
        "result": {
            "success": True,
            "view": "brief",
            "instructions": instructions,
            "page": {
                "completed": completed,
                "complete": cursor is None,
                "next_cursor": cursor,
            },
        },
    }


def test_completed_identities_are_credited_while_the_batch_continues() -> None:
    variables: dict[str, Any] = {"loaded_skills": ["earlier"]}
    output = _page(
        [
            _item("brevity", "b"),
            _item("gobby:references/a.md", "a"),
            _item("restraint", "r", final=False),
        ],
        ["brevity", "gobby:references/a.md"],
        "b.next",
    )

    detect_mcp_call(_after_get_skill(output), variables, SESSION_ID)

    assert variables["loaded_skills"] == ["earlier", "brevity"]
    assert variables["loaded_skill_references"] == ["gobby:references/a.md"]


def test_identity_without_its_end_marker_on_the_page_is_not_credited() -> None:
    variables: dict[str, Any] = {}
    output = _page([_item("brevity", "b", final=False)], ["brevity"], None)

    detect_mcp_call(_after_get_skill(output), variables, SESSION_ID)

    assert variables.get("loaded_skills", []) == []


def test_failed_batch_result_credits_nothing() -> None:
    variables: dict[str, Any] = {}
    output = {"success": False, "result": _page([_item("brevity", "b")], ["brevity"], None)}

    detect_mcp_call(_after_get_skill(output), variables, SESSION_ID)

    assert variables.get("loaded_skills", []) == []


def test_unavailable_identities_are_noted_so_they_are_not_requested_again() -> None:
    variables: dict[str, Any] = {}
    output = _page([_item("brevity", "b")], ["brevity"], None)
    output["result"]["unavailable"] = [
        {
            "identity": "gobby:references/a.md",
            "error_code": "not_found",
            "message": "File not found",
        }
    ]

    detect_mcp_call(_after_get_skill(output), variables, SESSION_ID)

    assert variables["loaded_skills"] == ["brevity"]
    assert variables["unavailable_instructions"] == ["gobby:references/a.md"]
