"""AGY hook contract regression tests."""

from __future__ import annotations

from typing import Any

import pytest

import gobby.adapters.agy_contract as agy_contract
from gobby.adapters.agy_contract import (
    AGY_EVENT_MAP,
    AGY_HOOK_ALIASES,
    AGY_HOOK_CONTRACTS,
    AGY_HOOK_NAMES,
    apply_agy_payload_aliases,
    get_agy_contract,
    normalize_agy_tool_call,
)
from gobby.hooks.events import HookEventType
from gobby.hooks.normalization import normalize_tool_fields, tool_input_error

pytestmark = pytest.mark.unit


def test_agy_hook_contract_maps_supported_events() -> None:
    assert AGY_HOOK_NAMES == (
        "PreInvocation",
        "PreToolUse",
        "PostToolUse",
        "PostInvocation",
        "Stop",
    )
    assert AGY_EVENT_MAP == {
        "PreInvocation": HookEventType.BEFORE_AGENT,
        "PreToolUse": HookEventType.BEFORE_TOOL,
        "PostToolUse": HookEventType.AFTER_TOOL,
        "PostInvocation": HookEventType.AFTER_AGENT,
        "Stop": HookEventType.STOP,
    }


def test_agy_pre_tool_contract_blocks_tool_calls() -> None:
    contract = AGY_HOOK_CONTRACTS["PreToolUse"]

    assert contract.blocks_tool_call is True
    assert contract.event_type is HookEventType.BEFORE_TOOL


def test_agy_aliases_resolve_to_contracts() -> None:
    assert AGY_HOOK_ALIASES["before_agent"] == "PreInvocation"
    assert AGY_HOOK_ALIASES["after_agent"] == "PostInvocation"
    assert AGY_HOOK_ALIASES["pre_tool_use"] == "PreToolUse"
    assert AGY_HOOK_ALIASES["post_tool_use"] == "PostToolUse"
    assert AGY_HOOK_ALIASES["stop"] == "Stop"
    assert get_agy_contract("pre_tool_use") is get_agy_contract("PreToolUse")
    assert get_agy_contract("unknown") is None


class TestAgyPayloadAliases:
    def test_declares_agy_local_alias_table(self) -> None:
        aliases = getattr(agy_contract, "AGY_PAYLOAD_ALIASES", None)
        assert isinstance(aliases, dict)
        assert aliases["conversationId"] == "session_id"
        assert aliases["transcriptPath"] == "transcript_path"
        assert aliases["workspacePaths"] == "workspace_paths"
        assert aliases["artifactDirectoryPath"] == "artifact_directory_path"
        assert aliases["modelName"] == "model"
        assert aliases["stepIdx"] == "step_idx"
        assert aliases["invocationNum"] == "invocation_num"
        assert aliases["initialNumSteps"] == "initial_num_steps"
        assert aliases["executionNum"] == "execution_num"
        assert aliases["terminationReason"] == "termination_reason"
        assert aliases["fullyIdle"] == "fully_idle"

    def test_decode_agy_tool_args_passthrough_and_json_string(self) -> None:
        decode = getattr(agy_contract, "decode_agy_tool_args", None)
        assert callable(decode)
        native: dict[str, Any] = {"CommandLine": "ls -la", "Cwd": "/repo"}
        encoded = '{"CommandLine": "pwd"}'

        assert decode(native) == native
        assert decode(encoded) == {"CommandLine": "pwd"}
        assert decode("not-json") == "not-json"

    def test_parse_agy_command_exit_reads_anchored_sentence(self) -> None:
        parse = getattr(agy_contract, "parse_agy_command_exit", None)
        assert callable(parse)
        live_zero = (
            "Created At: 2026-08-22T03:21:26-05:00\n"
            "Completed At: 2026-08-22T03:21:26-05:00\n\n"
            "The command exited with code 0.\nOutput:\ntotal 8\n"
        )
        live_nonzero = (
            "Created At: 2026-08-22T03:26:19-05:00\n"
            "Completed At: 2026-08-22T03:26:19-05:00\n\n"
            "The command exited with code 7.\nOutput:\nboom\n\n"
        )
        legacy_indented = "\t\t\t\tThe command exited with code 0.\n\t\t\t\tOutput:\n"

        assert parse(live_zero) == 0
        assert parse(live_nonzero) == 7
        assert parse(legacy_indented) == 0
        assert parse("Process failed with exit code 7") is None
        assert parse("no sentence") is None
        assert parse(None) is None

    def test_agy_tool_map_normalizes_snake_case_call_names(self) -> None:
        tool_map = getattr(agy_contract, "AGY_TOOL_MAP", None)
        assert isinstance(tool_map, dict)
        assert tool_map["list_dir"] == "Ls"
        assert tool_map["run_command"] == "Bash"
        assert tool_map["view_file"] == "Read"
        assert tool_map["find_by_name"] == "Glob"
        assert tool_map["call_mcp_tool"] == "mcp__gobby__call_tool"
        assert "write_to_file" in tool_map
        assert tool_map["write_to_file"] == "Write"
        assert "replace_file_content" in tool_map
        assert tool_map["replace_file_content"] == "Edit"
        assert "grep_search" in tool_map
        assert tool_map["grep_search"] == "Grep"

    @pytest.mark.parametrize(
        ("arguments", "expected_input"),
        [
            (
                {"server_name": "gobby-skills", "tool_name": "get_skill"},
                {"server_name": "gobby-skills", "tool_name": "get_skill"},
            ),
            (
                '{"server_name":"gobby-tasks","tool_name":"create_task"}',
                {"server_name": "gobby-tasks", "tool_name": "create_task"},
            ),
            ({}, {}),
            # Undecodable arguments stay the sender's string so hook
            # normalization marks them unavailable (#23168).
            ('{"server_name":', '{"server_name":'),
            ("[1]", "[1]"),
            (None, None),
        ],
    )
    def test_normalize_agy_mcp_arguments(
        self,
        arguments: dict[str, Any] | str | None,
        expected_input: dict[str, Any] | str | None,
    ) -> None:
        provider_input = {
            "ServerName": "gobby",
            "ToolName": "call_tool",
            "Arguments": arguments,
        }

        normalized = normalize_agy_tool_call("call_mcp_tool", provider_input)

        assert normalized["tool_name"] == "mcp__gobby__call_tool"
        if expected_input is None:
            assert normalized["tool_input"] == provider_input
        else:
            assert normalized["tool_input"] == expected_input
        assert normalized["_raw_tool_input"] == provider_input

    @pytest.mark.parametrize(
        "provider_input",
        [
            {"ServerName": "gobby", "Arguments": {}},
            {"ToolName": "call_tool", "Arguments": {}},
            {"ServerName": "gobby", "ToolName": "call_tool"},
            "not-an-envelope",
        ],
    )
    def test_normalize_agy_incomplete_mcp_envelope_uses_wrapper_fallback(
        self,
        provider_input: Any,
    ) -> None:
        normalized = normalize_agy_tool_call("call_mcp_tool", provider_input)

        assert normalized["tool_name"] == "mcp__gobby__call_tool"
        assert normalized["tool_input"] == provider_input

    @pytest.mark.parametrize(
        ("tool_call", "tool_name"),
        [
            ({"name": "write_to_file", "args": '{"TargetFile": "/repo/a.py", "Co'}, "Write"),
            (
                {
                    "name": "call_mcp_tool",
                    "args": {
                        "ServerName": "gobby",
                        "ToolName": "call_tool",
                        "Arguments": '{"server_name": "gobby-tasks", "tool_na',
                    },
                },
                "mcp__gobby__call_tool",
            ),
        ],
        ids=["tool-args", "mcp-arguments"],
    )
    def test_truncated_agy_args_reach_hooks_marked(
        self, tool_call: dict[str, Any], tool_name: str
    ) -> None:
        data = normalize_tool_fields(apply_agy_payload_aliases({"toolCall": tool_call}))

        assert data["tool_name"] == tool_name
        assert "tool_input" not in data
        assert tool_input_error(data) == {"field": "tool_input", "code": "invalid_json"}

    def test_force_continue_limit_is_a_positive_int(self) -> None:
        limit = getattr(agy_contract, "AGY_FORCE_CONTINUE_LIMIT", None)
        assert isinstance(limit, int)
        assert limit > 0
