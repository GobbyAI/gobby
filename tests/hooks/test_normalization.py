"""Tests for shared MCP field normalization."""

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks._normalization_shell import (
    has_mutating_output_redirection,
    shell_token_values,
    strip_output_redirections,
    tokenize_shell_command,
)
from gobby.hooks.normalization import normalize_mcp_fields, normalize_tool_fields
from gobby.mcp_proxy._call_tool_wrapper import canonicalize_call_tool_wrapper

pytestmark = pytest.mark.unit


class TestMcpPrefixParsing:
    """Tests for mcp__<server>__<tool> prefix parsing (Step 1a)."""

    def test_parses_standard_mcp_prefix(self) -> None:
        data = {"tool_name": "mcp__gobby-tasks__create_task"}
        result = normalize_mcp_fields(data)
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "create_task"

    def test_does_not_overwrite_existing_mcp_tool(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "mcp_tool": "already_set",
        }
        result = normalize_mcp_fields(data)
        # mcp_tool was already present, so prefix parsing should not overwrite
        assert result["mcp_tool"] == "already_set"

    def test_non_mcp_tool_unchanged(self) -> None:
        data = {"tool_name": "Read", "tool_input": {"file": "foo.py"}}
        result = normalize_mcp_fields(data)
        assert "mcp_server" not in result
        assert "mcp_tool" not in result

    def test_empty_tool_name(self) -> None:
        data = {"tool_name": ""}
        result = normalize_mcp_fields(data)
        assert "mcp_server" not in result
        assert "mcp_tool" not in result

    def test_malformed_prefix_only_two_parts(self) -> None:
        data = {"tool_name": "mcp__incomplete"}
        result = normalize_mcp_fields(data)
        # Only 2 parts after split, no mcp_server/mcp_tool set
        assert "mcp_server" not in result
        assert "mcp_tool" not in result


class TestSingleUnderscoreNormalization:
    """Tests for mcp_<server>_<tool> → mcp__<server>__<tool> normalization (Step 1a-pre)."""

    def test_single_underscore_call_tool(self) -> None:
        data = {"tool_name": "mcp_gobby_call_tool"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__call_tool"

    def test_single_underscore_list_tools(self) -> None:
        data = {"tool_name": "mcp_gobby_list_tools"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__list_tools"

    def test_single_underscore_list_mcp_servers(self) -> None:
        data = {"tool_name": "mcp_gobby_list_mcp_servers"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__list_mcp_servers"

    def test_single_underscore_get_tool_schema(self) -> None:
        data = {"tool_name": "mcp_gobby_get_tool_schema"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__get_tool_schema"

    def test_single_underscore_sets_mcp_server_and_tool(self) -> None:
        """After normalization, the prefix parsing should extract mcp_server/mcp_tool."""
        data = {"tool_name": "mcp_gobby_call_tool"}
        result = normalize_mcp_fields(data)
        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "call_tool"

    def test_double_underscore_unchanged(self) -> None:
        data = {"tool_name": "mcp__gobby__call_tool"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__call_tool"

    def test_non_mcp_prefix_unchanged(self) -> None:
        data = {"tool_name": "Read"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "Read"

    def test_bare_mcp_underscore_no_tool(self) -> None:
        """mcp_ with no further underscore should be left alone."""
        data = {"tool_name": "mcp_gobby"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp_gobby"

    def test_single_underscore_non_gobby_server(self) -> None:
        data = {"tool_name": "mcp_context7_get_docs"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__context7__get_docs"

    def test_single_underscore_hyphenated_server(self) -> None:
        data = {"tool_name": "mcp_gobby-tasks_claim_task"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby-tasks__claim_task"
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "claim_task"

    def test_single_underscore_call_tool_inner_extraction(self) -> None:
        """Single-underscore call_tool should still extract inner server/tool."""
        data = {
            "tool_name": "mcp_gobby_call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "create_task"},
        }
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__call_tool"
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "create_task"

    def test_full_pipeline_qwen_single_underscore(self) -> None:
        """End-to-end: Qwen-style single underscore through normalize_tool_fields."""
        data = {
            "function_name": "mcp_gobby_call_tool",
            "parameters": {"server_name": "gobby-memory", "tool_name": "create_memory"},
        }
        normalize_tool_fields(data)
        assert data["tool_name"] == "mcp__gobby__call_tool"
        assert data["mcp_server"] == "gobby-memory"
        assert data["mcp_tool"] == "create_memory"


class TestTripleUnderscoreNormalization:
    """Tests for droid <server>___<tool> MCP normalization."""

    def test_triple_underscore_sets_canonical_tool_name(self) -> None:
        data = {"tool_name": "gobby___list_mcp_servers"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__list_mcp_servers"
        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "list_mcp_servers"

    def test_canonical_mcp_name_is_idempotent(self) -> None:
        data = {"tool_name": "mcp__gobby__list_mcp_servers"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__list_mcp_servers"
        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "list_mcp_servers"

    def test_single_underscore_regression(self) -> None:
        data = {"tool_name": "mcp_gobby_list_mcp_servers"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby__list_mcp_servers"
        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "list_mcp_servers"

    def test_pascal_case_native_tool_name_passes_through(self) -> None:
        data = {"tool_name": "Read"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "Read"
        assert "mcp_server" not in result
        assert "mcp_tool" not in result

    def test_server_names_with_underscore_split_on_triple_separator(self) -> None:
        data = {"tool_name": "gobby_tasks___claim_task"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby_tasks__claim_task"
        assert result["mcp_server"] == "gobby_tasks"
        assert result["mcp_tool"] == "claim_task"

    def test_server_names_with_hyphen_split_on_triple_separator(self) -> None:
        data = {"tool_name": "gobby-tasks___claim_task"}
        result = normalize_mcp_fields(data)
        assert result["tool_name"] == "mcp__gobby-tasks__claim_task"
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "claim_task"


class TestCallToolExtraction:
    """Tests for call_tool / mcp__gobby__call_tool inner extraction (Step 1b)."""

    def test_mcp_gobby_call_tool_overrides_prefix(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-memory", "tool_name": "add_memory"},
        }
        result = normalize_mcp_fields(data)
        # Inner values override prefix-parsed "gobby" / "call_tool"
        assert result["tool_name"] == "mcp__gobby__call_tool"
        assert result["mcp_server"] == "gobby-memory"
        assert result["mcp_tool"] == "add_memory"

    def test_plain_call_tool_sets_from_input(self) -> None:
        data = {
            "tool_name": "call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "list_tasks"},
        }
        result = normalize_mcp_fields(data)
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "list_tasks"

    @pytest.mark.parametrize("argument_field", ["arguments", "args"])
    def test_nested_wrapper_route_matches_proxy_aliases(self, argument_field: str) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                argument_field: {
                    "server_name": "gobby-tasks",
                    "tool_name": "escalate_task",
                    "arguments": {"task_id": "#42"},
                }
            },
        }

        result = normalize_mcp_fields(data)

        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "escalate_task"

    def test_nested_arguments_take_precedence_over_args_alias(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "arguments": {
                    "server_name": "arguments-server",
                    "tool_name": "arguments-tool",
                },
                "args": {"server_name": "args-server", "tool_name": "args-tool"},
            },
        }

        result = normalize_mcp_fields(data)

        assert result["mcp_server"] == "arguments-server"
        assert result["mcp_tool"] == "arguments-tool"

    def test_top_level_route_fields_independently_override_nested_route(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "top-server",
                "arguments": {
                    "server_name": "nested-server",
                    "tool_name": "nested-tool",
                },
            },
        }

        result = normalize_mcp_fields(data)

        assert result["mcp_server"] == "top-server"
        assert result["mcp_tool"] == "nested-tool"

    @pytest.mark.parametrize(
        "tool_input",
        [
            {
                "arguments": {"server_name": "arguments-server", "tool_name": "arguments-tool"},
                "args": {"server_name": "args-server", "tool_name": "args-tool"},
            },
            {
                "server_name": "top-server",
                "arguments": {"server_name": "nested-server", "tool_name": "nested-tool"},
            },
            {
                "arguments": None,
                "args": {"server_name": "args-server", "tool_name": "args-tool"},
            },
            {
                "arguments": {},
                "args": {"server_name": "ignored-server", "tool_name": "ignored-tool"},
            },
        ],
    )
    def test_route_precedence_matches_proxy_canonicalizer(self, tool_input: dict[str, Any]) -> None:
        data = {"tool_name": "mcp__gobby__call_tool", "tool_input": tool_input}

        result = normalize_mcp_fields(data)
        canonical = canonicalize_call_tool_wrapper(
            server_name=tool_input.get("server_name"),
            tool_name=tool_input.get("tool_name"),
            arguments=tool_input.get("arguments"),
            args=tool_input.get("args"),
        )

        assert result.get("mcp_server") == (canonical.server_name or "gobby")
        assert result.get("mcp_tool") == (canonical.tool_name or "call_tool")

    def test_stringified_nested_wrapper_route_matches_proxy(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "arguments": '{"server_name":"gobby-tasks","tool_name":"escalate_task"}'
            },
        }

        result = normalize_mcp_fields(data)

        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "escalate_task"

    def test_malformed_nested_wrapper_preserves_top_level_route(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "escalate_task",
                "arguments": "{not-json",
            },
        }

        result = normalize_mcp_fields(data)

        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "escalate_task"

    @pytest.mark.parametrize("tool_input", ["not-an-object", ["not", "an", "object"]])
    def test_malformed_tool_input_is_safe(self, tool_input: object) -> None:
        data = {"tool_name": "mcp__gobby__call_tool", "tool_input": tool_input}

        result = normalize_mcp_fields(data)

        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "call_tool"

    def test_plain_call_tool_preserves_existing(self) -> None:
        data = {
            "tool_name": "call_tool",
            "tool_input": {"server_name": "inner-server", "tool_name": "inner-tool"},
            "mcp_server": "external-server",
            "mcp_tool": "external-tool",
        }
        result = normalize_mcp_fields(data)
        # Plain call_tool should NOT overwrite externally-set values
        assert result["mcp_server"] == "external-server"
        assert result["mcp_tool"] == "external-tool"

    def test_call_tool_missing_inner_fields(self) -> None:
        data = {
            "tool_name": "call_tool",
            "tool_input": {},
        }
        result = normalize_mcp_fields(data)
        assert "mcp_server" not in result
        assert "mcp_tool" not in result

    def test_call_tool_none_tool_input(self) -> None:
        data = {
            "tool_name": "call_tool",
            "tool_input": None,
        }
        result = normalize_mcp_fields(data)
        assert "mcp_server" not in result


class TestToolOutputNormalization:
    """Tests for tool_result / tool_response → tool_output (Step 2)."""

    def test_normalizes_tool_result(self) -> None:
        data = {"tool_result": "success"}
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == "success"

    def test_normalizes_tool_response(self) -> None:
        data = {"tool_response": {"status": "ok"}}
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == {"status": "ok"}

    def test_tool_result_takes_precedence_over_tool_response(self) -> None:
        data = {"tool_result": "from_result", "tool_response": "from_response"}
        result = normalize_mcp_fields(data)
        # tool_result is checked first, so it wins
        assert result["tool_output"] == "from_result"

    def test_existing_tool_output_not_overwritten(self) -> None:
        data = {"tool_result": "from_result", "tool_output": "already_set"}
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == "already_set"

    def test_no_tool_result_or_response(self) -> None:
        data = {"tool_name": "Read"}
        result = normalize_mcp_fields(data)
        assert "tool_output" not in result

    def test_string_tool_output_parsed_to_dict(self) -> None:
        """Claude Code sends tool_response as JSON string — should be parsed."""
        data = {
            "tool_response": '{"success": true, "result": {"id": "abc-123", "ref": "#42"}}',
        }
        result = normalize_mcp_fields(data)
        assert isinstance(result["tool_output"], dict)
        assert result["tool_output"]["success"] is True
        assert result["tool_output"]["result"]["id"] == "abc-123"

    def test_tool_response_envelope_uses_structured_content(self) -> None:
        data = {
            "tool_response": {
                "content": [{"type": "text", "text": '{"success": false, "error": "bad args"}'}],
                "structuredContent": {
                    "success": False,
                    "error": "bad args",
                    "result": {"ref": "#42"},
                },
                "isError": False,
            }
        }
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == {
            "success": False,
            "error": "bad args",
            "result": {"ref": "#42"},
        }

    def test_tool_response_envelope_parses_text_json_without_structured_content(self) -> None:
        data = {
            "tool_response": {
                "content": [{"type": "text", "text": '{"success": false, "error": "bad args"}'}],
                "isError": False,
            }
        }
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == {"success": False, "error": "bad args"}

    def test_tool_response_envelope_parses_get_skill_text_payload(self) -> None:
        data = {
            "tool_response": {
                "content": [
                    {
                        "type": "text",
                        "text": '{"result": {"success": true, "skill": {"name": "brevity"}}}',
                    }
                ],
                "isError": False,
            }
        }
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == {"result": {"success": True, "skill": {"name": "brevity"}}}

    def test_tool_output_envelope_parses_json_output_payload(self) -> None:
        data = {
            "tool_output": {
                "output": '{"result": {"success": true, "skill": {"name": "brevity"}}}',
            }
        }
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == {"result": {"success": True, "skill": {"name": "brevity"}}}

    def test_tool_output_envelope_ignores_non_dict_json_output_payload(self) -> None:
        data = {"tool_output": {"output": '["not", "an", "object"]'}}
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == {"output": '["not", "an", "object"]'}

    def test_tool_output_unwrap_stops_at_max_depth(self) -> None:
        output: dict[str, object] = {"status": "deep"}
        for _ in range(12):
            output = {"output": json.dumps(output)}
        data = {"tool_output": output}

        result = normalize_mcp_fields(data)

        assert isinstance(result["tool_output"], dict)
        assert "output" in result["tool_output"]

    def test_string_tool_output_non_json_left_as_string(self) -> None:
        """Non-JSON tool output (e.g. plain text) should remain a string."""
        data = {"tool_response": "Error: file not found"}
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == "Error: file not found"

    def test_string_tool_output_json_array_left_as_string(self) -> None:
        """JSON arrays should not be coerced (only dicts are useful)."""
        data = {"tool_response": "[1, 2, 3]"}
        result = normalize_mcp_fields(data)
        assert result["tool_output"] == "[1, 2, 3]"


class TestCombinedNormalization:
    """Tests verifying all normalizations work together."""

    def test_full_mcp_call_with_result(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "Test"},
            },
            "tool_response": {"id": "task-123"},
        }
        result = normalize_mcp_fields(data)
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "create_task"
        assert result["tool_output"] == {"id": "task-123"}

    def test_native_codex_mcp_post_tool_use_payload(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "Test"},
            },
            "tool_response": {
                "content": [
                    {
                        "type": "text",
                        "text": '{"success": true, "result": {"ref": "#42"}}',
                    }
                ],
                "isError": False,
            },
        }

        result = normalize_tool_fields(data)

        assert result["tool_name"] == "mcp__gobby__call_tool"
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "create_task"
        assert result["tool_input"]["arguments"] == {"title": "Test"}
        assert result["tool_output"] == {"success": True, "result": {"ref": "#42"}}

    def test_mutates_in_place(self) -> None:
        data = {"tool_name": "mcp__s__t"}
        returned = normalize_mcp_fields(data)
        assert returned is data
        assert data["mcp_server"] == "s"


# ═══════════════════════════════════════════════════════════════════════
# normalize_tool_fields — field alias tests
# ═══════════════════════════════════════════════════════════════════════


class TestFieldAliases:
    """Tests for CLI-specific field alias normalization (Phase 1)."""

    def test_function_name_to_tool_name(self) -> None:
        """Qwen sends function_name instead of tool_name."""
        data = {"function_name": "write_file"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "write_file"

    def test_function_name_does_not_overwrite_tool_name(self) -> None:
        data = {"function_name": "write_file", "tool_name": "Write"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "Write"

    def test_toolName_to_tool_name(self) -> None:
        """camelCase toolName is normalized to tool_name."""
        data = {"toolName": "Read"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "Read"

    def test_toolName_does_not_overwrite_tool_name(self) -> None:
        data = {"toolName": "Read", "tool_name": "CustomRead"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "CustomRead"

    def test_toolArgs_string_parsed_to_tool_input(self) -> None:
        """toolArgs as a JSON string is parsed to tool_input."""
        data = {"toolArgs": '{"path": "/foo.py"}'}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"path": "/foo.py", "file_path": "/foo.py"}

    def test_toolArgs_object_to_tool_input(self) -> None:
        """toolArgs as a dict should pass through without JSON parsing."""
        data = {"toolArgs": {"path": "/foo.py"}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"path": "/foo.py", "file_path": "/foo.py"}

    def test_toolArgs_invalid_json_string_kept_as_string(self) -> None:
        """Invalid JSON in toolArgs should be kept as-is."""
        data = {"toolArgs": "not valid json"}
        normalize_tool_fields(data)
        assert data["tool_input"] == "not valid json"

    def test_toolArgs_does_not_overwrite_tool_input(self) -> None:
        data = {"toolArgs": '{"a": 1}', "tool_input": {"b": 2}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"b": 2}

    def test_parameters_to_tool_input(self) -> None:
        """Qwen sends parameters instead of tool_input."""
        data = {"parameters": {"file": "test.py"}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"file": "test.py"}

    def test_args_to_tool_input(self) -> None:
        """Qwen fallback: args → tool_input."""
        data = {"args": {"cmd": "ls"}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"cmd": "ls"}

    def test_parameters_takes_precedence_over_args(self) -> None:
        data = {"parameters": {"from_params": True}, "args": {"from_args": True}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"from_params": True}


class TestMcpContextFlattening:
    """Tests for mcp_context {} → mcp_server / mcp_tool (Qwen MCP)."""

    def test_mcp_context_flattened(self) -> None:
        data = {
            "mcp_context": {"server_name": "gobby-memory", "tool_name": "recall"},
        }
        normalize_tool_fields(data)
        assert data["mcp_server"] == "gobby-memory"
        assert data["mcp_tool"] == "recall"

    def test_mcp_context_does_not_overwrite_existing(self) -> None:
        data = {
            "mcp_context": {"server_name": "inner", "tool_name": "inner_tool"},
            "mcp_server": "already_set",
        }
        normalize_tool_fields(data)
        assert data["mcp_server"] == "already_set"
        assert data["mcp_tool"] == "inner_tool"

    def test_mcp_context_empty_dict_ignored(self) -> None:
        data = {"mcp_context": {}}
        normalize_tool_fields(data)
        assert "mcp_server" not in data
        assert "mcp_tool" not in data

    def test_mcp_context_non_dict_ignored(self) -> None:
        data = {"mcp_context": "not a dict"}
        normalize_tool_fields(data)
        assert "mcp_server" not in data


class TestNormalizeToolFieldsAlias:
    """Verify normalize_tool_fields runs the full pipeline."""

    def test_is_callable(self) -> None:
        assert callable(normalize_tool_fields)

    def test_runs_mcp_prefix_parsing(self) -> None:
        """Phase 2 (MCP prefix) should also run via normalize_tool_fields."""
        data = {"tool_name": "mcp__gobby-tasks__create_task"}
        normalize_tool_fields(data)
        assert data["mcp_server"] == "gobby-tasks"
        assert data["mcp_tool"] == "create_task"

    def test_runs_output_normalization(self) -> None:
        """Phase 2 (tool_result → tool_output) should also run."""
        data = {"tool_result": "ok"}
        normalize_tool_fields(data)
        assert data["tool_output"] == "ok"

    def test_mutates_in_place(self) -> None:
        data = {"toolName": "Read"}
        returned = normalize_tool_fields(data)
        assert returned is data
        assert data["tool_name"] == "Read"

    def test_combined_camelcase_style(self) -> None:
        """Full camelCase-style event through normalize_tool_fields."""
        data = {
            "toolName": "mcp__gobby__call_tool",
            "toolArgs": '{"server_name": "gobby-memory", "tool_name": "create_memory"}',
            "tool_result": "ok",
        }
        normalize_tool_fields(data)
        assert data["tool_name"] == "mcp__gobby__call_tool"
        assert data["tool_input"] == {
            "server_name": "gobby-memory",
            "tool_name": "create_memory",
        }
        assert data["mcp_server"] == "gobby-memory"
        assert data["mcp_tool"] == "create_memory"
        assert data["tool_output"] == "ok"


class TestWriteNormalization:
    """Tests for canonical write input normalization."""

    def test_write_change_list_populates_file_path(self) -> None:
        data = {
            "tool_name": "Write",
            "tool_input": [{"path": "/file.txt", "content": "new content"}],
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Write"
        assert data["tool_input"] == {
            "changes": [{"path": "/file.txt", "content": "new content"}],
            "file_path": "/file.txt",
        }

    def test_write_change_list_populates_file_paths_for_multiple_files(self) -> None:
        data = {
            "tool_name": "Write",
            "tool_input": [
                {"path": "/file-a.txt", "content": "a"},
                {"path": "/file-b.txt", "content": "b"},
            ],
        }

        normalize_tool_fields(data)

        assert data["tool_input"]["file_path"] == "/file-a.txt"
        assert data["tool_input"]["file_paths"] == ["/file-a.txt", "/file-b.txt"]

    def test_apply_patch_normalized_to_write(self) -> None:
        data = {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/main.py\n"
                "@@\n"
                "-print('old')\n"
                "+print('new')\n"
                "*** End Patch\n"
            ),
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Write"
        assert data["_original_tool_name"] == "apply_patch"
        assert data["tool_input"]["patch"].startswith("*** Begin Patch")
        assert data["tool_input"]["file_path"] == "src/main.py"

    def test_apply_patch_multi_file_populates_file_paths(self) -> None:
        data = {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/main.py\n"
                "@@\n"
                "*** Add File: docs/plan.md\n"
                "+hello\n"
                "*** End Patch\n"
            ),
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Write"
        assert data["tool_input"]["file_path"] == "src/main.py"
        assert data["tool_input"]["file_paths"] == ["src/main.py", "docs/plan.md"]

    def test_apply_patch_without_paths_preserves_patch_only(self) -> None:
        data = {
            "tool_name": "apply_patch",
            "tool_input": "*** Begin Patch\n*** End Patch\n",
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Write"
        assert data["tool_input"] == {"patch": "*** Begin Patch\n*** End Patch\n"}
        assert "file_path" not in data["tool_input"]


class TestCanonicalToolMetadata:
    """Tests for derived canonical tool semantics."""

    def test_read_tool_sets_canonical_read_fields(self) -> None:
        data = {"tool_name": "Read", "tool_input": {"file_path": "/repo/main.py"}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_file_path"] == "/repo/main.py"
        assert data["canonical_tool_confidence"] == "high"
        assert data["canonical_code_navigation_action"] == "read"
        assert data["canonical_code_navigation_broad"] is True
        assert data["canonical_source_read_scope"] == "full_file"

    def test_exec_command_cat_sets_canonical_read_fields(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "cat src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_file_path"] == "src/app.py"
        assert data["canonical_file_paths"] == ["src/app.py"]
        assert data["canonical_code_navigation_action"] == "read"
        assert data["canonical_code_navigation_broad"] is True
        assert data["canonical_source_read_scope"] == "full_file"

    def test_exec_command_rg_sets_canonical_search_kind(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "rg session_lookup src"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_file_path"] == "src"
        assert data["canonical_file_paths"] == ["src"]
        assert data["canonical_code_navigation_action"] == "search"
        assert data["canonical_code_navigation_broad"] is True

    def test_exec_command_unclassified_shell_sets_canonical_execute_kind(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "uv run gobby build #15117"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "execute"
        assert data["canonical_tool_confidence"] == "high"
        assert "canonical_repo_mutation" not in data

    @pytest.mark.parametrize(
        ("command", "expected_kind"),
        [
            ('gcode grep "pattern" src -m 50', "search"),
            ('gcode search-content "query" src', "search"),
            ("gcode outline src/app.py", "read"),
            ("gcode symbol 00000000-0000-0000-0000-000000000000", "read"),
            ("gcode symbol-at src/app.py:42", "read"),
            ("gcode callers TaskValidator", "read"),
            ("gcode tree src", "read"),
            ("gcode repo-outline", "read"),
            ('gcode symbol-at src/app.py:42 ; gcode grep "a" src -m 10', "search"),
            # A gcode navigation piped to a read-only filter is still navigation:
            # `gcode symbol <id> | jq -r .source` is the documented way to read
            # symbol source, and `| head`/`| rg` are common too. Without this the
            # per-turn nav flag never sets and source reads stay capped at 40 lines.
            (
                "gcode symbol 00000000-0000-0000-0000-000000000000 | jq -r .source",
                "read",
            ),
            ("gcode outline src/app.py | head -40", "read"),
            ('gcode grep "pattern" src -m 50 | rg fn', "search"),
            # Batched all-gcode sequences are still pure navigation: every
            # segment is side-effect free, so `;`/`&&` joins keep the exemption.
            ('gcode grep "a" src -m 10 ; gcode grep "b" src -m 10', "search"),
            ('gcode grep "a" src -m 10 && gcode outline src/app.py', "search"),
            (
                "gcode outline src/app.py ; gcode symbol 00000000-0000-0000-0000-000000000000",
                "read",
            ),
            # Plain-text echo markers between gcode calls are neutral separators,
            # and redirects to benign sinks like /dev/null mutate nothing.
            ("gcode symbol 00000000-0000-0000-0000-000000000000 ; echo done", "read"),
            ('gcode grep "a" src ; echo done', "search"),
            ('gcode grep "a" src -m 10 ; echo === ; gcode grep "b" src -m 10', "search"),
            ('gcode grep "pattern" src -m 50 2>/dev/null', "search"),
            ("gcode outline src/app.py 2>/dev/null", "read"),
            ('gcode grep "a" src 2>/dev/null | rg fn', "search"),
            # Fd duplication (gobby-#17743) rebinds descriptors without opening
            # files, so it keeps the exemption, as does a benign `>&` sink.
            ('gcode grep "a" src 2>&1', "search"),
            ("gcode outline src/app.py 2>&1", "read"),
            ('gcode grep "a" src 1>&2', "search"),
            ('gcode grep "a" src >&2', "search"),
            ('gcode grep "a" src 2>&-', "search"),
            ('gcode grep "a" src >& /dev/null', "search"),
            (
                "gcode symbol 00000000-0000-0000-0000-000000000000 | jq -r .source 2>&1",
                "read",
            ),
        ],
    )
    def test_exec_command_gcode_navigation_is_canonical(
        self, command: str, expected_kind: str
    ) -> None:
        data = {"tool_name": "exec_command", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == expected_kind
        assert data["canonical_code_index_navigation"] is True
        assert data["canonical_code_index_command"].startswith("gcode ")

    @pytest.mark.parametrize(
        "command",
        [
            "gcode outline src/app.py && rm -rf build",
            "gcode grep pattern src || true",
            "gcode outline src/app.py | tee out.txt",
            "gcode symbol 00000000-0000-0000-0000-000000000000 | jq -r .source > out.txt",
            "gcode symbol 00000000-0000-0000-0000-000000000000 > out.txt | head -1",
            'gcode grep "a" src ; echo $(date)',
            'gcode grep "a" src ; echo done > out.txt',
            'gcode grep "a" src > results.txt',
            'gcode grep "a" src > out.txt 2>&1',
        ],
    )
    def test_exec_command_gcode_with_side_effects_loses_pure_navigation(self, command: str) -> None:
        # `&&`/`;`/`||` joining a *non-gcode*, non-neutral command (possibly
        # side-effecting) drops pure navigation; only all-gcode sequences,
        # plain echo markers, and `|` pipelines of read-only filters keep it.
        data = {"tool_name": "exec_command", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] in {"read", "search", "write"}
        assert "canonical_code_index_navigation" not in data

    def test_exec_command_gcode_with_fd_dup_keeps_pure_navigation(self) -> None:
        # gobby-#17743: `2>&1` scans as a single fd-duplication token, so it
        # neither splits the segment nor counts as a mutating redirection.
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": 'gcode grep "a" src 2>&1'},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_index_navigation"] is True

    def test_exec_command_gcode_with_broad_read_loses_pure_navigation(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "gcode outline src/app.py && cat src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_file_paths"] == ["src/app.py"]
        assert data["canonical_code_navigation_broad"] is True
        assert "canonical_code_index_navigation" not in data

    def test_exec_command_git_grep_sets_broad_search(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "git grep TaskValidator src"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_action"] == "search"
        assert data["canonical_code_navigation_broad"] is True

    def test_exec_command_find_sets_broad_search(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "find src -name '*.py'"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_file_path"] == "src"
        assert data["canonical_code_navigation_action"] == "search"
        assert data["canonical_code_navigation_broad"] is True

    def test_exec_command_search_populates_visible_paths(self) -> None:
        examples = [
            ("rg foo .claude/memory", ".claude/memory"),
            ("grep foo .claude/memory/file.md", ".claude/memory/file.md"),
            ("git grep foo -- .claude/memory", ".claude/memory"),
            ("find .claude/memory -type f", ".claude/memory"),
        ]
        for command, expected_path in examples:
            data = {"tool_name": "exec_command", "tool_input": {"command": command}}

            normalize_tool_fields(data)

            assert data["canonical_tool_kind"] == "search"
            assert data["canonical_file_path"] == expected_path

    def test_exec_command_tight_sed_source_read_sets_narrow_context(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "sed -n '10,49p' src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_file_path"] == "src/app.py"
        assert data["canonical_source_line_count"] == 40
        assert data["canonical_code_navigation_broad"] is False
        assert data["canonical_narrow_source_context"] is True

    def test_exec_command_compound_cd_rebases_search_paths(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "cd dir && rg pattern src"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_file_path"] == "dir/src"
        assert data["canonical_file_paths"] == ["dir/src"]

    @pytest.mark.parametrize("separator", [" && ", "; ", "\n"])
    def test_exec_command_pipeline_preserves_parent_cd_for_following_segment(
        self, separator: str
    ) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {
                "command": (
                    f"cd scratch && grep needle before.txt | head -1{separator}rg later after.txt"
                )
            },
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_file_paths"] == [
            "scratch/before.txt",
            "scratch/after.txt",
        ]

    def test_exec_command_pipeline_local_cd_does_not_change_sibling_or_parent_cwd(
        self,
    ) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {
                "command": (
                    "cd scratch && cd nested | grep needle < sibling.txt ; rg later after.txt"
                )
            },
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_file_paths"] == [
            "scratch/sibling.txt",
            "scratch/after.txt",
        ]

    @pytest.mark.parametrize("separator", [" || ", " & "])
    def test_exec_command_pipeline_keeps_uncertain_separator_cwd_reset(
        self, separator: str
    ) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {
                "command": (
                    f"cd scratch && grep needle before.txt | head -1{separator}rg later after.txt"
                )
            },
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_file_paths"] == ["scratch/before.txt", "after.txt"]

    def test_exec_command_compound_cd_rebases_newline_sed_path(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "cd dir\nsed -n '1,60p' app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_file_path"] == "dir/app.py"
        assert data["canonical_code_navigation_broad"] is True

    def test_exec_command_compound_cd_rebases_semicolon_cat_path(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "cd dir; cat app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_file_path"] == "dir/app.py"

    def test_exec_command_wide_sed_source_read_sets_broad_context(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "sed -n '10,60p' src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_source_line_count"] == 51
        assert data["canonical_code_navigation_broad"] is True
        assert data["canonical_source_read_scope"] == "line_range"

    def test_exec_command_head_source_read_respects_line_limit(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "head -n 40 src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_source_line_count"] == 40
        assert data["canonical_code_navigation_broad"] is False

    def test_exec_command_redirection_sets_canonical_write_fields(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "printf hello > src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "src/app.py"
        assert data["tool_input"]["file_path"] == "src/app.py"

    @pytest.mark.parametrize(
        ("command", "expected_kind"),
        [
            ("grep -r pattern src 2>/dev/null", "search"),
            ("find src -name '*.py' 2>/dev/null", "search"),
            ("cat src/app.py > /dev/null", "read"),
            ("rg pattern src >/dev/null 2>&1", "search"),
            ("rg pattern src >&/dev/null", "search"),
            ("head -n 40 src/app.py 2>>/dev/null", "read"),
        ],
    )
    def test_exec_command_benign_redirect_is_not_write(
        self, command: str, expected_kind: str
    ) -> None:
        # Redirecting output to /dev/null-style sinks mutates nothing; the
        # segment keeps its base read/search classification.
        data = {"tool_name": "exec_command", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == expected_kind
        assert "canonical_repo_mutation" not in data
        assert "/dev/null" not in data.get("canonical_file_paths", [])

    @pytest.mark.parametrize(
        "command",
        [
            "grep pattern src > results.txt",
            "grep pattern src 2> errors.log",
            "echo hi > src/notes.txt",
            # csh-style `>&file` writes stdout+stderr to the file; `>&2file` is
            # a redirect to the file `2file` (non-numeric word), not fd dup.
            "grep pattern src >&results.txt",
            "grep pattern src >&2file",
            "> out.txt",
        ],
    )
    def test_exec_command_redirect_to_real_file_is_still_write(self, command: str) -> None:
        data = {"tool_name": "exec_command", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True

    def test_exec_command_bare_fd_dup_segment_is_execute(self) -> None:
        # A segment reduced to only fd-dup tokens must classify, not crash.
        data = {"tool_name": "exec_command", "tool_input": {"command": "echo hi; <&3"}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert "canonical_repo_mutation" not in data

    def test_exec_command_pipeline_tee_sets_canonical_write_fields(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "printf hello | tee src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "src/app.py"

    def test_exec_command_heredoc_with_output_redirection_sets_write(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "cat <<'EOF' > src/app.py\nhello\nEOF"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_path"] == "src/app.py"

    def test_heredoc_body_waits_for_logical_command_continuation(self) -> None:
        command = "cat > first.txt <<'EOF' &&\nprintf done > visible.txt\nbody > ignored.txt\nEOF"
        data = {"tool_name": "Bash", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_paths"] == ["first.txt", "visible.txt"]

    def test_quoted_heredoc_append_attributes_only_redirect_target(self) -> None:
        # Heredoc bodies are stdin data; lines like ``-> None:`` must never
        # scan as output redirections that mint fake edited-file paths.
        command = (
            "cat >> tests/foo.py <<'EOF'\n"
            "def make(row) -> None:\n"
            "    ns: SimpleNamespace = SimpleNamespace()\n"
            "    items: list[SimpleNamespace] = []\n"
            "    mapping: dict[str, int] = {}\n"
            "    assert row >= 3\n"
            "EOF"
        )
        data = {"tool_name": "Bash", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_paths"] == ["tests/foo.py"]

    def test_command_after_heredoc_body_still_classifies(self) -> None:
        command = "cat > notes.txt <<'EOF'\nplain > text\nEOF\nsed -i 's/a/b/' src/app.py"
        data = {"tool_name": "Bash", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_paths"] == ["notes.txt", "src/app.py"]

    def test_tab_indented_heredoc_body_is_skipped(self) -> None:
        command = "cat <<-EOF > src/app.py\n\tvalue > threshold\n\tEOF"
        data = {"tool_name": "Bash", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_paths"] == ["src/app.py"]

    def test_exec_command_sed_in_place_sets_canonical_write_fields(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "sed -i 's/old/new/' src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "src/app.py"

    def test_exec_command_cd_sed_in_place_rebases_write_path(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "cd src && sed -i 's/old/new/' app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_path"] == "src/app.py"

    def test_exec_command_quoted_output_operator_is_plain_argument(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "echo '>' src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert "canonical_file_path" not in data

    @pytest.mark.parametrize(
        "command",
        [
            "echo 'src/app.py'",
            "git commit -m 'touch src/app.py'",
            "cargo test path.rs",
        ],
    )
    def test_exec_command_source_like_arguments_do_not_false_positive(self, command: str) -> None:
        data = {"tool_name": "exec_command", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert "canonical_file_path" not in data

    def test_write_tool_sets_canonical_write_fields(self) -> None:
        data = {"tool_name": "Write", "tool_input": {"file_path": "/repo/main.py"}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "/repo/main.py"

    def test_write_tool_scratchpad_path_is_not_repo_mutation(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        scratchpad = tmp_path / "gobby-agent-scratchpad-session" / "notes.md"
        data = {
            "tool_name": "Write",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"file_path": str(scratchpad)},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is False
        assert data["canonical_file_path"] == str(scratchpad)

    def test_write_tool_repo_path_is_repo_mutation(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        data = {
            "tool_name": "Write",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"file_path": "src/app.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "src/app.py"

    def test_write_tool_mixed_scratchpad_and_repo_paths_is_repo_mutation(self, tmp_path) -> None:
        repo = tmp_path / "repo"
        scratchpad = tmp_path / "gobby-agent-scratchpad-session" / "notes.md"
        data = {
            "tool_name": "Write",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"file_paths": [str(scratchpad), "src/app.py"]},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_paths"] == [str(scratchpad), "src/app.py"]

    def test_edit_tool_sets_canonical_write_fields(self) -> None:
        data = {"tool_name": "Edit", "tool_input": {"file_path": "/repo/main.py"}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "/repo/main.py"

    def test_apply_patch_sets_canonical_write_paths(self) -> None:
        data = {
            "tool_name": "apply_patch",
            "tool_input": (
                "*** Begin Patch\n"
                "*** Update File: src/main.py\n"
                "@@\n"
                "*** Add File: docs/plan.md\n"
                "+hello\n"
                "*** End Patch\n"
            ),
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Write"
        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "src/main.py"
        assert data["canonical_file_paths"] == ["src/main.py", "docs/plan.md"]

    def test_exec_command_truncate_sets_all_canonical_write_paths(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "truncate -r ref.txt a.txt b.txt c.txt"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_path"] == "a.txt"
        assert data["canonical_file_paths"] == ["a.txt", "b.txt", "c.txt"]

    def test_exec_command_truncate_respects_end_of_options_marker(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "truncate -s 0 -- --dash-prefixed.txt normal.txt"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_path"] == "--dash-prefixed.txt"
        assert data["canonical_file_paths"] == ["--dash-prefixed.txt", "normal.txt"]

    def test_exec_command_rg_default_gobby_logs_is_not_repo_scoped(
        self, tmp_path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        monkeypatch.delenv("GOBBY_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))
        data = {
            "tool_name": "exec_command",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": "rg error ~/.gobby/logs/daemon.log"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_broad"] is True
        assert data["canonical_code_navigation_repo_scope"] is False

    def test_exec_command_grep_explicit_gobby_home_logs_is_not_repo_scoped(
        self, tmp_path, monkeypatch
    ) -> None:
        repo = tmp_path / "repo"
        gobby_home = tmp_path / "gobby-home"
        log_path = gobby_home / "logs" / "daemon.log"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        data = {
            "tool_name": "exec_command",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": f"grep error {log_path}"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_broad"] is True
        assert data["canonical_code_navigation_repo_scope"] is False

    @pytest.mark.parametrize("command", ["rg error src", "rg error"])
    def test_exec_command_rg_repo_search_is_repo_scoped(self, command: str, tmp_path) -> None:
        repo = tmp_path / "repo"
        data = {
            "tool_name": "exec_command",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": command},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_broad"] is True
        assert data["canonical_code_navigation_repo_scope"] is True


def test_gcode_callees_and_graph_view_are_navigation() -> None:
    for command, expected_kind in (
        ("gcode callees TaskValidator", "read"),
        ("gcode graph view --view=fcg Derived", "read"),
        ("gcode graph view --view=class-hierarchy Derived", "read"),
    ):
        data = {"tool_name": "exec_command", "tool_input": {"command": command}}
        normalize_tool_fields(data)
        assert data["canonical_tool_kind"] == expected_kind
        assert data["canonical_code_index_navigation"] is True
        assert data["canonical_code_index_command"].startswith("gcode ")

    for command in (
        "gcode graph clear",
        "gcode graph rebuild",
        "gcode graph sync-file --file src/app.py",
        "gcode graph cleanup-orphans",
    ):
        data = {"tool_name": "exec_command", "tool_input": {"command": command}}
        normalize_tool_fields(data)
        assert "canonical_code_index_navigation" not in data


class TestFdDuplicationTokens:
    """Tests for fd-duplication (`N>&M`) scanning in the shell tokenizer (gobby-#17743)."""

    @pytest.mark.parametrize(
        ("command", "expected_values"),
        [
            ("gcode grep a src 2>&1", ["gcode", "grep", "a", "src", "2>&1"]),
            ("foo 1>&2", ["foo", "1>&2"]),
            ("foo >&2", ["foo", ">&2"]),
            ("foo 12>&13", ["foo", "12>&13"]),
            ("foo 2>&-", ["foo", "2>&-"]),
            ("foo >&-", ["foo", ">&-"]),
            ("cat 0<&3", ["cat", "0<&3"]),
            ("cat <&3", ["cat", "<&3"]),
        ],
    )
    def test_fd_duplication_scans_as_single_token(
        self, command: str, expected_values: list[str]
    ) -> None:
        tokens = tokenize_shell_command(command)

        assert shell_token_values(tokens) == expected_values
        assert has_mutating_output_redirection(tokens) is False

    def test_fd_dup_with_trailing_word_is_not_fd_dup(self) -> None:
        # bash reads `2>&1x` as an ambiguous redirect, not fd duplication;
        # the split tokenization keeps the fail-closed mutating classification.
        tokens = tokenize_shell_command("foo 2>&1x")

        assert shell_token_values(tokens) == ["foo", "2>", "&", "1x"]
        assert has_mutating_output_redirection(tokens) is True

    def test_redirect_both_to_file_is_mutating(self) -> None:
        # `>&word` with a non-numeric word writes stdout+stderr to the file.
        tokens = tokenize_shell_command("foo >&2file")

        assert shell_token_values(tokens) == ["foo", ">&", "2file"]
        assert has_mutating_output_redirection(tokens) is True

    def test_quoted_fd_dup_is_a_word(self) -> None:
        tokens = tokenize_shell_command("grep '2>&1' log.txt")

        assert shell_token_values(tokens) == ["grep", "2>&1", "log.txt"]
        assert tokens[1].quoted is True
        assert has_mutating_output_redirection(tokens) is False

    def test_strip_output_redirections_drops_fd_dup_without_target(self) -> None:
        tokens = tokenize_shell_command("rg pattern src >/dev/null 2>&1")

        stripped = shell_token_values(strip_output_redirections(tokens))

        assert stripped == ["rg", "pattern", "src"]


class TestHeredocTokenization:
    """Heredoc bodies are stdin data and must never tokenize as shell syntax."""

    def test_heredoc_body_lines_are_not_tokenized(self) -> None:
        command = "cat >> tests/foo.py <<'EOF'\ndef f(x) -> None:\n    y >= 2\nEOF\necho done"

        tokens = tokenize_shell_command(command)

        assert shell_token_values(tokens) == [
            "cat",
            ">>",
            "tests/foo.py",
            "<<",
            "EOF",
            "\n",
            "echo",
            "done",
        ]

    def test_multiple_heredocs_consume_delimiters_in_order(self) -> None:
        command = "cat <<A <<B\nbody > a\nA\nbody > b\nB\nls"

        tokens = tokenize_shell_command(command)

        assert shell_token_values(tokens) == ["cat", "<<", "A", "<<", "B", "\n", "ls"]

    def test_unterminated_heredoc_swallows_remaining_lines(self) -> None:
        command = "cat <<EOF > out.txt\nstill > body\nnever closed"

        tokens = tokenize_shell_command(command)

        assert shell_token_values(tokens) == ["cat", "<<", "EOF", ">", "out.txt", "\n"]

    def test_herestring_does_not_open_a_heredoc(self) -> None:
        command = "sort <<< 'b a'\necho done > out.txt"

        tokens = tokenize_shell_command(command)

        assert shell_token_values(tokens) == [
            "sort",
            "<<<",
            "b a",
            "\n",
            "echo",
            "done",
            ">",
            "out.txt",
        ]

    def test_quoted_newline_does_not_start_heredoc_body(self) -> None:
        command = 'cat <<EOF "first\nsecond"\nbody\nEOF'

        tokens = tokenize_shell_command(command)

        assert shell_token_values(tokens) == ["cat", "<<", "EOF", "first\nsecond", "\n"]


class TestToolErrorDetection:
    """Tests for Phase 3: structured tool outcome normalization."""

    def test_run_shell_command_is_canonicalized_to_bash(self) -> None:
        data = {"tool_name": "run_shell_command", "tool_result": "Exit code: 0"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "Bash"

    def test_exec_command_is_canonicalized_to_bash(self) -> None:
        data = {"tool_name": "exec_command", "tool_result": "Exit code: 0"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "Bash"

    def test_bash_nonzero_exit_code_text_remains_unknown(self) -> None:
        """Agent-readable output is not a machine outcome signal."""
        data = {
            "tool_name": "Bash",
            "tool_result": "command not found\nExit code: 1",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_bash_exit_code_127(self) -> None:
        """An exit-code phrase in display text is not parsed."""
        data = {
            "tool_name": "Bash",
            "tool_result": "bash: foo: command not found\nExit code: 127",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_bash_exit_code_detection_is_case_insensitive_and_bounded(self) -> None:
        """Case and number shape do not make display text authoritative."""
        data = {
            "tool_name": "Bash",
            "tool_result": "failed\nEXIT-CODE 2",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

        ignored = {
            "tool_name": "Bash",
            "tool_result": "failed\nexit code 12345",
        }
        normalize_tool_fields(ignored)
        assert "is_error" not in ignored
        assert ignored["tool_outcome"]["status"] == "unknown"

    def test_bash_zero_exit_code_no_is_error(self) -> None:
        """Bash tool_result with zero exit code → is_error not set."""
        data = {
            "tool_name": "Bash",
            "tool_result": "success output\nExit code: 0",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_bash_no_exit_code_in_output(self) -> None:
        """Bash output without exit code pattern → is_error not set."""
        data = {
            "tool_name": "Bash",
            "tool_result": "some normal output",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_non_bash_tool_unaffected(self) -> None:
        """Non-shell tools should not get is_error from output text."""
        data = {
            "tool_name": "Read",
            "tool_result": "Error: Exit code: 1",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_pre_existing_is_error_not_overridden(self) -> None:
        """If is_error is already set (e.g. by adapter), don't override."""
        data = {
            "tool_name": "Bash",
            "tool_result": "Exit code: 0",
            "is_error": True,  # adapter already decided this is an error
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True
        assert data["tool_outcome"]["status"] == "failed"

    def test_pre_existing_is_error_false_not_overridden(self) -> None:
        """If is_error is explicitly False, don't override with detection."""
        data = {
            "tool_name": "Bash",
            "tool_result": "Exit code: 1",
            "is_error": False,
        }
        normalize_tool_fields(data)
        assert data["is_error"] is False
        assert data["tool_outcome"]["status"] == "succeeded"

    def test_lowercase_bash_tool_name(self) -> None:
        """Lowercase shell aliases do not enable display-text inference."""
        data = {
            "tool_name": "bash",
            "tool_result": "error\nExit code: 2",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_shell_tool_name(self) -> None:
        """Shell aliases do not enable display-text inference."""
        data = {
            "tool_name": "shell",
            "tool_result": "Exit code: 1",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_run_command_tool_name(self) -> None:
        """Native shell names do not enable display-text inference."""
        data = {
            "tool_name": "run_command",
            "tool_result": "exit code: 1",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_exec_command_tool_name(self) -> None:
        """Exec aliases do not enable display-text inference."""
        data = {
            "tool_name": "exec_command",
            "tool_result": "exit code: 1",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_tool_output_used_when_tool_result_absent(self) -> None:
        """Direct display output is not parsed for an exit-code phrase."""
        data = {
            "tool_name": "Bash",
            "tool_output": "failed\nexit code: 1",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data
        assert data["tool_outcome"]["status"] == "unknown"

    def test_structured_non_string_tool_result_is_authoritative(self) -> None:
        """A structured exit code is authoritative."""
        data = {
            "tool_name": "Bash",
            "tool_result": {"exit_code": 1, "output": "fail"},
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True
        assert data["tool_outcome"] == {
            "status": "failed",
            "exit_code": 1,
            "provenance": "tool_output.exit_code",
        }


class TestEndToEndRuleMatch:
    """Verify normalized data matches rule 'when' expressions."""

    def test_create_memory_rule_match(self) -> None:
        """Data from mcp__gobby__call_tool with create_memory should match
        the clear-memory-review-on-create rule's when expression:
        event.data.get('mcp_tool') == 'create_memory' and
        event.data.get('mcp_server') == 'gobby-memory'
        """
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-memory",
                "tool_name": "create_memory",
                "arguments": {"content": "test"},
            },
        }
        normalize_tool_fields(data)

        # Simulate rule engine `when` evaluation
        assert data.get("mcp_tool") == "create_memory"
        assert data.get("mcp_server") == "gobby-memory"

    def test_nested_call_tool_target_is_rule_visible(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "arguments": {
                    "server_name": "gobby-tasks",
                    "tool_name": "escalate_task",
                    "arguments": {"task_id": "#42", "reason": "blocked"},
                }
            },
        }

        normalize_tool_fields(data)

        assert data.get("mcp_server") == "gobby-tasks"
        assert data.get("mcp_tool") == "escalate_task"

    def test_qwen_create_memory_rule_match(self) -> None:
        """Same rule match with Qwen-style fields."""
        data = {
            "function_name": "call_tool",
            "parameters": {
                "server_name": "gobby-memory",
                "tool_name": "create_memory",
            },
        }
        normalize_tool_fields(data)

        assert data.get("mcp_tool") == "create_memory"
        assert data.get("mcp_server") == "gobby-memory"

    def test_after_tool_without_tool_input_does_not_match(self) -> None:
        """after_tool (post-tool-use) omits tool_input in Claude Code.
        Without tool_input, normalization falls back to prefix parsing which
        yields mcp_server='gobby' and mcp_tool='call_tool' — neither matches
        the clear-memory-review-on-create rule condition.
        This is the root cause of the memory-review-gate never clearing.
        """
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_result": '{"success": true}',
            # No tool_input — this is what Claude Code sends for post-tool-use
        }
        normalize_tool_fields(data)

        # Prefix parsing yields "gobby" / "call_tool", NOT the inner server/tool
        assert data.get("mcp_server") == "gobby"
        assert data.get("mcp_tool") == "call_tool"
        # Therefore the rule condition does NOT match
        assert data.get("mcp_tool") != "create_memory"
        assert data.get("mcp_server") != "gobby-memory"

    def test_camelcase_create_memory_rule_match(self) -> None:
        """Same rule match, but with camelCase fields and JSON string args."""
        data = {
            "toolName": "mcp__gobby__call_tool",
            "toolArgs": '{"server_name": "gobby-memory", "tool_name": "create_memory"}',
        }
        normalize_tool_fields(data)

        assert data.get("mcp_tool") == "create_memory"
        assert data.get("mcp_server") == "gobby-memory"


class TestStringArgumentCoercion:
    """Tests for auto-coercing stringified arguments in call_tool."""

    def test_string_arguments_coerced_to_dict(self) -> None:
        """call_tool with JSON string arguments → parsed to dict + flag set."""
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": '{"title": "Test task", "session_id": "#1"}',
            },
        }
        normalize_mcp_fields(data)
        assert data["tool_input"]["arguments"] == {"title": "Test task", "session_id": "#1"}
        assert data["_input_coerced"] is True

    def test_dict_arguments_unchanged(self) -> None:
        """call_tool with dict arguments → no coercion, no flag."""
        data = {
            "tool_name": "call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "Test task"},
            },
        }
        normalize_mcp_fields(data)
        assert data["tool_input"]["arguments"] == {"title": "Test task"}
        assert "_input_coerced" not in data

    def test_invalid_json_string_left_as_is(self) -> None:
        """Unparseable string arguments → left unchanged, no flag."""
        data = {
            "tool_name": "call_tool",
            "tool_input": {
                "server_name": "s",
                "tool_name": "t",
                "arguments": "not valid json{",
            },
        }
        normalize_mcp_fields(data)
        assert data["tool_input"]["arguments"] == "not valid json{"
        assert "_input_coerced" not in data

    def test_json_array_string_not_coerced(self) -> None:
        """JSON string that parses to a list (not dict) → left as-is."""
        data = {
            "tool_name": "call_tool",
            "tool_input": {
                "server_name": "s",
                "tool_name": "t",
                "arguments": "[1, 2, 3]",
            },
        }
        normalize_mcp_fields(data)
        assert data["tool_input"]["arguments"] == "[1, 2, 3]"
        assert "_input_coerced" not in data

    def test_no_arguments_key_no_flag(self) -> None:
        """call_tool without arguments key → no coercion."""
        data = {
            "tool_name": "call_tool",
            "tool_input": {
                "server_name": "s",
                "tool_name": "t",
            },
        }
        normalize_mcp_fields(data)
        assert "_input_coerced" not in data

    def test_non_call_tool_unaffected(self) -> None:
        """Non-call_tool with string arguments → no coercion attempted."""
        data = {
            "tool_name": "Read",
            "tool_input": {"arguments": '{"key": "val"}'},
        }
        normalize_mcp_fields(data)
        assert data["tool_input"]["arguments"] == '{"key": "val"}'
        assert "_input_coerced" not in data

    def test_coercion_through_normalize_tool_fields(self) -> None:
        """Full pipeline: camelCase-style stringified args through normalize_tool_fields."""
        data = {
            "toolName": "mcp__gobby__call_tool",
            "toolArgs": '{"server_name": "gobby-tasks", "tool_name": "create_task", "arguments": "{\\"title\\": \\"Test\\"}"}',
        }
        normalize_tool_fields(data)
        assert data["tool_input"]["arguments"] == {"title": "Test"}
        assert data["_input_coerced"]
        assert data["mcp_server"] == "gobby-tasks"
        assert data["mcp_tool"] == "create_task"


class TestUnexpandedShellReferencePaths:
    """Shell path tokens holding unexpanded references never become canonical paths."""

    @pytest.mark.parametrize(
        "command",
        [
            "curl -s -o $SP/out.json http://localhost:60887/api/config/template",
            "curl -s -o ${SP}/out.json http://localhost:60887/api/config/template",
            "curl -s --output=$SP/out.json http://localhost:60887/api/config/template",
        ],
    )
    def test_curl_variable_output_is_pathless_write(self, command: str) -> None:
        data = {"tool_name": "Bash", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert "canonical_file_path" not in data
        assert "canonical_file_paths" not in data

    def test_variable_redirection_target_stays_pathless(self) -> None:
        # extract_redirection_paths already drops variable targets; the segment
        # then normalizes as the documented path-less execute residual. Pin that
        # no phantom path appears either way.
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo probe > $OUT/log.txt"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert "canonical_file_path" not in data
        assert "canonical_file_paths" not in data

    def test_special_parameter_git_add_has_unknown_mutation_scope(self) -> None:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": "git add $@"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert data["canonical_repo_mutation"] is True
        assert data["_canonical_repo_mutation_scope_unknown"] is True
        assert "canonical_file_path" not in data
        assert "canonical_file_paths" not in data

    def test_variable_tee_target_is_pathless_write(self) -> None:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": "tee $OUT/log.txt"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert "canonical_file_path" not in data
        assert "canonical_file_paths" not in data

    def test_literal_curl_output_still_extracts_path(self) -> None:
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "curl -s -o out.json http://localhost:60887/api"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_path"] == "out.json"
        assert data["canonical_file_paths"] == ["out.json"]

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("$SP/out.json", True),
            ("${SP}/out.json", True),
            ("$(mktemp -d)/out.json", True),
            ("$1/out.json", True),
            ("$@", True),
            ("$*", True),
            ("$?", True),
            ("$#", True),
            ("$$", True),
            ("$!", True),
            ("$-", True),
            ("out$.json", False),
            ("price$.md", False),
            ("plain/out.json", False),
        ],
    )
    def test_reference_detector_boundaries(self, path: str, expected: bool) -> None:
        from gobby.hooks._normalization_canonical import (
            _contains_unexpanded_shell_reference,
        )

        assert _contains_unexpanded_shell_reference(path) is expected


class TestExternalNavigationScope:
    """Navigation over CLI state homes and loop headers resolves repo scope correctly."""

    def test_for_loop_over_external_logs_is_not_repo_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        gobby_home = tmp_path / "gobby-home"
        monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
        command = (
            f"for f in {gobby_home}/logs/errors.log {gobby_home}/logs/daemon.log; "
            'do grep "credential_generation" "$f" | tail -2; done'
        )
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": command},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_repo_scope"] is False

    def test_for_loop_over_repo_files_stays_repo_scoped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": 'for f in src/a.py src/b.py; do grep error "$f"; done'},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_repo_scope"] is True

    def test_do_prefixed_command_classifies_as_search(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": 'for f in src/a.py; do grep error "$f"; done'},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"

    def test_agent_state_home_search_is_not_repo_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        monkeypatch.delenv("GOBBY_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": "grep -iE 'memory|deny' ~/.claude/settings.json"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_repo_scope"] is False

    def test_source_read_under_agent_home_is_not_repo_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        repo = tmp_path / "repo"
        monkeypatch.delenv("GOBBY_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"command": f"cat {home}/.codex/hooks/session_handler.py"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "read"
        assert data["canonical_code_navigation_repo_scope"] is False

    def test_read_only_inline_python_is_execute(self) -> None:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    'python3 -c \'import json; data = json.load(open("runs.json")); '
                    "print(len(data))'"
                )
            },
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert not data.get("canonical_repo_mutation")

    def test_mutating_inline_python_stays_write(self) -> None:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": 'python3 -c \'open("out.txt", "w").write("x")\''},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True

    def test_read_only_with_open_inline_python_is_execute(self) -> None:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "python3 -c 'with open(\"log.txt\") as fh: print(fh.read())'"
            },
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert not data.get("canonical_repo_mutation")

    def test_read_only_python_heredoc_is_execute(self) -> None:
        command = (
            "python3 - <<'PYEOF'\n"
            "import json\n"
            "with open('data.json') as fh:\n"
            "    rows = json.load(fh)\n"
            "print(len(rows))\n"
            "PYEOF"
        )
        data: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert not data.get("canonical_repo_mutation")

    def test_mutating_python_heredoc_stays_write(self) -> None:
        command = "python3 - <<'PYEOF'\nwith open('out.txt', 'w') as fh:\n    fh.write('x')\nPYEOF"
        data: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True

    def test_tmux_capture_pane_is_not_repo_mutation(self) -> None:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": "tmux -L gobby capture-pane -p -S -10000 -t 'gobby-1'"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert not data.get("canonical_repo_mutation")

    def test_external_report_write_is_not_repo_mutation(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        report = tmp_path / "reports" / "review-codex.md"
        data: dict[str, Any] = {
            "tool_name": "Write",
            "cwd": str(repo),
            "project_path": str(repo),
            "tool_input": {"file_path": str(report), "content": "verdict"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is False

    def test_python_script_execution_is_execute(self) -> None:
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": "uv run python scripts/generate_schema_identity.py --check"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert not data.get("canonical_repo_mutation")

    def test_worktree_under_agent_home_stays_repo_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.delenv("GOBBY_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))
        worktree = home / ".claude" / "worktrees" / "gobby"
        data: dict[str, Any] = {
            "tool_name": "Bash",
            "cwd": str(worktree),
            "project_path": str(worktree),
            "tool_input": {"command": "rg error src"},
        }

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "search"
        assert data["canonical_code_navigation_repo_scope"] is True
