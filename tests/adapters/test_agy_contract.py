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
    get_agy_contract,
)
from gobby.hooks.events import HookEventType

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

    def test_force_continue_limit_is_a_positive_int(self) -> None:
        limit = getattr(agy_contract, "AGY_FORCE_CONTINUE_LIMIT", None)
        assert isinstance(limit, int)
        assert limit > 0
