"""Tests for shared MCP field normalization."""

import pytest

from gobby.hooks.normalization import normalize_mcp_fields, normalize_tool_fields

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

    def test_full_pipeline_gemini_single_underscore(self) -> None:
        """End-to-end: Gemini-style single underscore through normalize_tool_fields."""
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


class TestCallToolExtraction:
    """Tests for call_tool / mcp__gobby__call_tool inner extraction (Step 1b)."""

    def test_mcp_gobby_call_tool_overrides_prefix(self) -> None:
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-memory", "tool_name": "add_memory"},
        }
        result = normalize_mcp_fields(data)
        # Inner values override prefix-parsed "gobby" / "call_tool"
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
        """Gemini sends function_name instead of tool_name."""
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
        """Gemini sends parameters instead of tool_input."""
        data = {"parameters": {"file": "test.py"}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"file": "test.py"}

    def test_args_to_tool_input(self) -> None:
        """Gemini fallback: args → tool_input."""
        data = {"args": {"cmd": "ls"}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"cmd": "ls"}

    def test_parameters_takes_precedence_over_args(self) -> None:
        data = {"parameters": {"from_params": True}, "args": {"from_args": True}}
        normalize_tool_fields(data)
        assert data["tool_input"] == {"from_params": True}


class TestMcpContextFlattening:
    """Tests for mcp_context {} → mcp_server / mcp_tool (Gemini MCP)."""

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

    def test_exec_command_rg_sets_canonical_search_kind(self) -> None:
        data = {
            "tool_name": "exec_command",
            "tool_input": {"command": "rg session_lookup src"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "search"
        assert "canonical_file_path" not in data

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

    def test_write_tool_sets_canonical_write_fields(self) -> None:
        data = {"tool_name": "Write", "tool_input": {"file_path": "/repo/main.py"}}

        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_repo_mutation"] is True
        assert data["canonical_file_path"] == "/repo/main.py"

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
            "tool_input": {"command": "truncate -s 0 a.txt b.txt c.txt"},
        }

        normalize_tool_fields(data)

        assert data["tool_name"] == "Bash"
        assert data["canonical_tool_kind"] == "write"
        assert data["canonical_file_path"] == "a.txt"
        assert data["canonical_file_paths"] == ["a.txt", "b.txt", "c.txt"]


class TestToolErrorDetection:
    """Tests for Phase 3: shell tool error detection from output text."""

    def test_run_shell_command_is_canonicalized_to_bash(self) -> None:
        data = {"tool_name": "run_shell_command", "tool_result": "Exit code: 0"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "Bash"

    def test_exec_command_is_canonicalized_to_bash(self) -> None:
        data = {"tool_name": "exec_command", "tool_result": "Exit code: 0"}
        normalize_tool_fields(data)
        assert data["tool_name"] == "Bash"

    def test_bash_nonzero_exit_code_sets_is_error(self) -> None:
        """Bash tool_result with non-zero exit code → is_error = True."""
        data = {
            "tool_name": "Bash",
            "tool_result": "command not found\nExit code: 1",
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_bash_exit_code_127(self) -> None:
        """Exit code 127 (command not found) detected."""
        data = {
            "tool_name": "Bash",
            "tool_result": "bash: foo: command not found\nExit code: 127",
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_bash_zero_exit_code_no_is_error(self) -> None:
        """Bash tool_result with zero exit code → is_error not set."""
        data = {
            "tool_name": "Bash",
            "tool_result": "success output\nExit code: 0",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data

    def test_bash_no_exit_code_in_output(self) -> None:
        """Bash output without exit code pattern → is_error not set."""
        data = {
            "tool_name": "Bash",
            "tool_result": "some normal output",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data

    def test_non_bash_tool_unaffected(self) -> None:
        """Non-shell tools should not get is_error from output text."""
        data = {
            "tool_name": "Read",
            "tool_result": "Error: Exit code: 1",
        }
        normalize_tool_fields(data)
        assert "is_error" not in data

    def test_pre_existing_is_error_not_overridden(self) -> None:
        """If is_error is already set (e.g. by adapter), don't override."""
        data = {
            "tool_name": "Bash",
            "tool_result": "Exit code: 0",
            "is_error": True,  # adapter already decided this is an error
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_pre_existing_is_error_false_not_overridden(self) -> None:
        """If is_error is explicitly False, don't override with detection."""
        data = {
            "tool_name": "Bash",
            "tool_result": "Exit code: 1",
            "is_error": False,
        }
        normalize_tool_fields(data)
        assert data["is_error"] is False

    def test_lowercase_bash_tool_name(self) -> None:
        """Lowercase 'bash' tool name should also be detected."""
        data = {
            "tool_name": "bash",
            "tool_result": "error\nExit code: 2",
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_shell_tool_name(self) -> None:
        """'shell' tool name should also be detected."""
        data = {
            "tool_name": "shell",
            "tool_result": "Exit code: 1",
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_run_command_tool_name(self) -> None:
        """'run_command' native tool name should also be detected."""
        data = {
            "tool_name": "run_command",
            "tool_result": "exit code: 1",
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_exec_command_tool_name(self) -> None:
        """'exec_command' tool name should also be detected."""
        data = {
            "tool_name": "exec_command",
            "tool_result": "exit code: 1",
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_tool_output_used_when_tool_result_absent(self) -> None:
        """Phase 2 normalizes tool_result → tool_output; Phase 3 checks tool_output."""
        data = {
            "tool_name": "Bash",
            "tool_output": "failed\nexit code: 1",
        }
        normalize_tool_fields(data)
        assert data["is_error"] is True

    def test_non_string_tool_result_ignored(self) -> None:
        """Dict tool_result (e.g. MCP JSON) should not be parsed for exit codes."""
        data = {
            "tool_name": "Bash",
            "tool_result": {"exit_code": 1, "output": "fail"},
        }
        normalize_tool_fields(data)
        assert "is_error" not in data


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

    def test_gemini_create_memory_rule_match(self) -> None:
        """Same rule match, but with Gemini-style fields."""
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
        assert data["_input_coerced"] is True
        assert data["mcp_server"] == "gobby-tasks"
        assert data["mcp_tool"] == "create_task"
