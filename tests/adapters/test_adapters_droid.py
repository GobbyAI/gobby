"""Tests for Factory Droid adapter hook translation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.adapters.droid import DroidAdapter
from gobby.adapters.droid_contract import DROID_EVENT_MAP, DROID_PASCAL_HOOK_NAMES
from gobby.hooks.events import HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit

DROID_COMMAND_OUTCOMES_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures/provider_contracts/droid/command-outcomes-0.174.0.json"
)


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

        assert event is not None
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
        assert event is not None
        assert event.event_type is HookEventType.BEFORE_TOOL
        assert event.session_id == "direct-session"

    def test_input_data_session_id_wins_over_top_level_camel_case(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {
                "hook_type": "SessionStart",
                "sessionId": "top-level-session",
                "input_data": {"session_id": "nested-session", "cwd": "/repo"},
            }
        )
        assert event is not None
        assert event.event_type is HookEventType.SESSION_START
        assert event.session_id == "nested-session"
        assert event.data["session_id"] == "nested-session"

    def test_input_data_camel_case_session_id_is_canonicalized(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {
                "hook_type": "SessionStart",
                "input_data": {"sessionId": "camel-session", "cwd": "/repo"},
            }
        )
        assert event is not None
        assert event.session_id == "camel-session"
        assert event.data["session_id"] == "camel-session"
        assert event.cwd == "/repo"

    def test_top_level_camel_case_session_id_fallback(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {
                "hook_type": "SessionStart",
                "sessionId": "top-level-session",
                "machineId": "21000000-0000-4000-8000-000000000001",
                "input_data": {"cwd": "/repo"},
            }
        )
        assert event is not None
        assert event.session_id == "top-level-session"
        assert event.machine_id == "21000000-0000-4000-8000-000000000001"
        assert event.data["session_id"] == "top-level-session"

    def test_blank_input_data_session_id_falls_back_to_top_level_id(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {
                "hook_type": "SessionStart",
                "sessionId": "top-level-session",
                "input_data": {"session_id": "   ", "cwd": "/repo"},
            }
        )

        assert event is not None
        assert event.session_id == "top-level-session"
        assert event.data["session_id"] == "top-level-session"

    def test_unknown_hook_type_falls_back_to_notification(self) -> None:
        adapter = DroidAdapter()
        event = adapter.translate_to_hook_event(
            {"hook_type": "FutureHook", "input_data": {"session_id": "future"}}
        )
        assert event is not None
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
        assert event is not None
        assert event.data["prompt"] == "Build it"
        assert event.data["tool_name"] == "mcp__gobby__list_mcp_servers"
        assert event.data["mcp_server"] == "gobby"
        assert event.data["mcp_tool"] == "list_mcp_servers"

    def test_live_post_tool_use_marks_execute_success_definitively(self) -> None:
        payload = json.loads(DROID_COMMAND_OUTCOMES_FIXTURE.read_text())
        native = {
            "hook_type": "PostToolUse",
            "input_data": payload["terminal_hook_observation"]["successful_post_tool_use"],
        }

        event = DroidAdapter().translate_to_hook_event(native)

        assert event is not None
        assert event.data["tool_name"] == "Bash"
        assert event.data["tool_output"].startswith("droid-zero-exit")
        assert event.metadata["is_failure"] is False

    def test_ambiguous_execute_output_does_not_infer_failure_from_text(self) -> None:
        event = DroidAdapter().translate_to_hook_event(
            {
                "hook_type": "Notification",
                "input_data": {
                    "tool_name": "Execute",
                    "tool_input": {"command": "sh -c 'exit 7'"},
                    "tool_response": "Error: Command failed (exit code: 7)",
                },
            }
        )

        assert event is not None
        assert event.data["tool_name"] == "Bash"
        assert "is_error" not in event.data
        assert "is_failure" not in event.metadata


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

    @pytest.mark.parametrize("blocking", ["deny", "block"])
    @pytest.mark.parametrize("override", [{"permission_decision": "allow"}, {"auto_approve": True}])
    def test_a_block_wins_over_a_permission_allow(
        self, blocking: str, override: dict[str, Any]
    ) -> None:
        """First-block-wins: an allow from a higher-priority rule cannot run the tool.

        #16670 fixed this fail-open in the Claude adapter only; Droid took
        response.permission_decision verbatim and applied the deny only when it
        was empty, so a lower-priority block was silently overridden.
        """
        result = DroidAdapter().translate_from_hook_response(
            HookResponse(decision=cast(Any, blocking), reason="No writes", **override),
            hook_type="PreToolUse",
        )

        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "No writes"

    def test_an_undenied_permission_allow_still_allows(self) -> None:
        result = DroidAdapter().translate_from_hook_response(
            HookResponse(decision="allow", permission_decision="allow"),
            hook_type="PreToolUse",
        )

        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

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

    def test_session_start_live_context_does_not_replay_persona(self) -> None:
        adapter = DroidAdapter()
        result = adapter.translate_from_hook_response(
            HookResponse(
                decision="allow",
                system_message="Gobby Session ID: #6273 (sess-live-123)",
                context="Claimed task refs: #15237 [in_progress]",
            ),
            hook_type="SessionStart",
        )

        assert "systemMessage" not in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Gobby Session ID: #6273 (sess-live-123)" in ctx
        assert "Claimed task refs: #15237 [in_progress]" in ctx
        assert "## Role" not in ctx
        assert "## Personality" not in ctx

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

    def test_flattened_hook_event_name_preserves_block_contract(self) -> None:
        hook_manager = MagicMock()
        hook_manager.handle.return_value = HookResponse(decision="block", reason="No writes")
        adapter = DroidAdapter(hook_manager=hook_manager)

        result = adapter.handle_native(
            {
                "hook_event_name": "PreToolUse",
                "source": "droid",
                "session_id": "direct-session",
                "tool_name": "Write",
            },
            hook_manager,
        )

        assert result == {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "No writes",
            },
        }
        handled_event = hook_manager.handle.call_args.args[0]
        assert handled_event.event_type is HookEventType.BEFORE_TOOL
        assert handled_event.session_id == "direct-session"
