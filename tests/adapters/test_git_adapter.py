"""Tests for the git reference-transaction guard adapter."""

from typing import Any, cast

import pytest

from gobby.adapters import GitAdapter
from gobby.adapters.base import ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
from gobby.adapters.git import GIT_BLOCK_FALLBACK_REASON
from gobby.hooks.events import HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit

PLATFORM_SESSION = "11111111-1111-4111-8111-111111111111"


def _native(input_data: dict[str, Any], platform_session_id: str | None = None) -> dict[str, Any]:
    native: dict[str, Any] = {"input_data": input_data}
    if platform_session_id is not None:
        native["_platform_session_id"] = platform_session_id
    return native


def test_git_adapter_reports_the_git_source() -> None:
    assert GitAdapter().source == SessionSource.GIT


def test_guard_input_becomes_a_before_tool_event() -> None:
    event = GitAdapter().translate_to_hook_event(
        _native(
            {
                "staged_paths": ["src/gobby/a.py", "docs/b.md"],
                "integration_branch": "0.5.0",
                "cwd": "/repo",
                "machine_id": "machine-1",
            },
            platform_session_id=PLATFORM_SESSION,
        )
    )

    assert event.event_type == HookEventType.BEFORE_TOOL
    assert event.source == SessionSource.GIT
    assert event.session_id == PLATFORM_SESSION
    assert event.machine_id == "machine-1"
    assert event.cwd == "/repo"
    assert event.data["tool_name"] == "Bash"
    assert event.data["staged_paths"] == ["src/gobby/a.py", "docs/b.md"]
    assert event.data["integration_branch"] == "0.5.0"
    assert event.metadata["_platform_session_id"] == PLATFORM_SESSION


def test_missing_platform_session_falls_back_to_the_input_session() -> None:
    event = GitAdapter().translate_to_hook_event(
        _native({"session_id": "external-9", "integration_branch": "0.5.0"})
    )

    assert event.session_id == "external-9"
    assert "_platform_session_id" not in event.metadata


def test_missing_session_everywhere_yields_an_empty_session() -> None:
    event = GitAdapter().translate_to_hook_event(_native({"integration_branch": "0.5.0"}))

    assert event.session_id == ""


def test_ordinary_commit_carries_no_integration_branch() -> None:
    event = GitAdapter().translate_to_hook_event(
        _native({"staged_paths": ["docs/b.md"]}, platform_session_id=PLATFORM_SESSION)
    )

    assert event.data["integration_branch"] is None
    assert event.data["tool_input"] == {"command": "git commit"}


def test_malformed_staged_paths_are_dropped_to_an_empty_list() -> None:
    event = GitAdapter().translate_to_hook_event(
        _native({"staged_paths": "docs/b.md", "integration_branch": ""})
    )

    assert event.data["staged_paths"] == []
    assert event.data["integration_branch"] is None


def test_only_an_explicit_allow_lands_the_update() -> None:
    response = GitAdapter().translate_from_hook_response(HookResponse(decision="allow"))

    assert response == {"decision": "allow"}


def test_deny_permission_refuses_even_under_an_allow_decision() -> None:
    response = GitAdapter().translate_from_hook_response(
        HookResponse(decision="allow", permission_decision="deny")
    )

    assert response["decision"] == "block"


def test_block_carries_the_rule_reason() -> None:
    response = GitAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="Code lands through land_commit.")
    )

    assert response["decision"] == "block"
    assert "land_commit" in response["reason"]


def test_block_without_a_reason_uses_the_loud_sentinel() -> None:
    response = GitAdapter().translate_from_hook_response(HookResponse(decision="block"))

    assert response == {"decision": "block", "reason": ADAPTER_EMPTY_BLOCK_REASON_SENTINEL}


@pytest.mark.parametrize("decision", ["ask", "defer", "continue", ""])
def test_any_other_decision_refuses_with_the_guard_fallback(decision: str) -> None:
    # The daemon's decision arrives untyped, so the adapter must refuse values outside the literals.
    response = GitAdapter().translate_from_hook_response(HookResponse(decision=cast(Any, decision)))

    assert response == {"decision": "block", "reason": GIT_BLOCK_FALLBACK_REASON}
