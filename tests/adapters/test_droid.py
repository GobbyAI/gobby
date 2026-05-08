"""Tests for Factory Droid adapter hook translation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.adapters.droid import DroidAdapter
from gobby.adapters.droid_contract import DROID_EVENT_MAP, DROID_PASCAL_HOOK_NAMES
from gobby.hooks.events import HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit


class TestDroidAdapterInit:
    def test_source_is_droid(self) -> None:
        adapter = DroidAdapter()
        assert adapter.source is SessionSource.DROID

    def test_init_with_hook_manager(self) -> None:
        hook_manager = MagicMock()
        adapter = DroidAdapter(hook_manager=hook_manager)
        assert adapter._hook_manager is hook_manager


class TestDroidTranslateToHookEvent:
    @pytest.mark.parametrize(
        ("hook_type", "event_type"),
        [
            ("PreToolUse", HookEventType.BEFORE_TOOL),
            ("PostToolUse", HookEventType.AFTER_TOOL),
            ("UserPromptSubmit", HookEventType.BEFORE_AGENT),
            ("Notification", HookEventType.NOTIFICATION),
            ("Stop", HookEventType.STOP),
            ("SubagentStop", HookEventType.SUBAGENT_STOP),
            ("PreCompact", HookEventType.PRE_COMPACT),
            ("SessionStart", HookEventType.SESSION_START),
            ("SessionEnd", HookEventType.SESSION_END),
        ],
    )
    def test_all_plan_hooks_map_to_unified_event_types(
        self,
        hook_type: str,
        event_type: HookEventType,
    ) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {
                "hook_type": hook_type,
                "source": "droid",
                "input_data": {"session_id": "droid-session", "cwd": "/repo"},
            }
        )

        assert event.event_type is event_type
        assert event.session_id == "droid-session"
        assert event.source is SessionSource.DROID
        assert event.cwd == "/repo"

    def test_event_map_matches_contract(self) -> None:
        adapter = DroidAdapter()
        assert adapter.EVENT_MAP == DROID_EVENT_MAP
        assert tuple(adapter.EVENT_MAP) == DROID_PASCAL_HOOK_NAMES

    def test_direct_invocation_uses_hook_event_name(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "direct-session",
                "tool_name": "Read",
            }
        )
        assert event.event_type is HookEventType.BEFORE_TOOL
        assert event.session_id == "direct-session"

    def test_unknown_hook_type_falls_back_to_notification(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {"hook_type": "FutureHook", "input_data": {"session_id": "future"}}
        )
        assert event.event_type is HookEventType.NOTIFICATION

    def test_normalizes_user_prompt_and_droid_mcp_tool_name(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {
                "hook_type": "UserPromptSubmit",
                "input_data": {
                    "session_id": "prompt-session",
                    "user_prompt": "Build it",
                    "tool_name": "gobby___list_mcp_servers",
                },
            }
        )
        assert event.data["prompt"] == "Build it"
        assert event.data["tool_name"] == "mcp__gobby__list_mcp_servers"
        assert event.data["mcp_server"] == "gobby"
        assert event.data["mcp_tool"] == "list_mcp_servers"


class TestDroidTranslateFromHookResponse:
    def test_allow_decision_omits_empty_hook_specific_output(self) -> None:
        adapter = DroidAdapter()
        result = adapter.translate_from_hook_response(
            HookResponse(decision="allow"),
            hook_type="PreToolUse",
        )
        assert result == {"continue": True}

    def test_pre_tool_use_block_uses_permission_decision(self) -> None:
        adapter = DroidAdapter()
        result = adapter.translate_from_hook_response(
            HookResponse(decision="block", reason="No writes"),
            hook_type="PreToolUse",
        )
        assert result == {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "No writes",
            },
        }

    def test_pre_tool_use_ask_and_updated_input(self) -> None:
        adapter = DroidAdapter()
        result = adapter.translate_from_hook_response(
            HookResponse(decision="ask", modified_input={"file_path": "/safe/path"}),
            hook_type="PreToolUse",
        )
        assert result["continue"] is True
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "ask"
        assert hso["updatedInput"] == {"file_path": "/safe/path"}

    @pytest.mark.parametrize(
        "hook_type", ["PostToolUse", "UserPromptSubmit", "Stop", "SubagentStop"]
    )
    def test_top_level_block_hooks_use_decision_block(self, hook_type: str) -> None:
        adapter = DroidAdapter()
        result = adapter.translate_from_hook_response(
            HookResponse(decision="deny", reason="Blocked"),
            hook_type=hook_type,
        )
        assert result == {"continue": True, "decision": "block", "reason": "Blocked"}

    def test_session_start_additional_context_carries_banner_once(self) -> None:
        adapter = DroidAdapter()
        result = adapter.translate_from_hook_response(
            HookResponse(
                decision="allow",
                system_message="Gobby Session ID: #100 (uuid-123)",
                context="Project context",
                metadata={"session_id": "uuid-123", "_first_hook_for_session": True},
            ),
            hook_type="SessionStart",
        )

        assert "systemMessage" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        assert hso["additionalContext"].count("Gobby Session ID: #100 (uuid-123)") == 1
        assert "Project context" in hso["additionalContext"]

    @pytest.mark.parametrize("hook_type", ["Notification", "PreCompact", "SessionEnd"])
    def test_none_style_denial_warns_without_blocking(self, hook_type: str) -> None:
        adapter = DroidAdapter()
        result = adapter.translate_from_hook_response(
            HookResponse(decision="deny", reason="Cannot block here"),
            hook_type=hook_type,
        )
        assert result == {"continue": True, "systemMessage": "Cannot block here"}


class TestDroidHandleNative:
    def test_handle_native_is_public_entrypoint(self) -> None:
        hook_manager = MagicMock()
        hook_manager.handle.return_value = HookResponse(decision="allow")
        adapter = DroidAdapter(hook_manager=hook_manager)

        result = adapter.handle_native(
            {
                "hook_type": "PreToolUse",
                "source": "droid",
                "input_data": {"session_id": "handled", "tool_name": "Read"},
            },
            hook_manager,
        )

        assert result == {"continue": True}
        handled_event = hook_manager.handle.call_args.args[0]
        assert handled_event.source is SessionSource.DROID
        assert handled_event.event_type is HookEventType.BEFORE_TOOL
