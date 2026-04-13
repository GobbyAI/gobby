"""Tests for chat_session_helpers."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import SyncHookJSONOutput

from gobby.servers.chat_session_helpers import (
    _FALLBACK_SYSTEM_PROMPT,
    _build_gobby_mcp_entry,
    _find_cli_path,
    _find_mcp_config,
    _find_project_root,
    _load_chat_system_prompt,
    _response_to_compact_output,
    _response_to_post_tool_output,
    _response_to_pre_tool_output,
    _response_to_prompt_output,
    _response_to_stop_output,
    _response_to_subagent_output,
    build_compaction_context,
)

pytestmark = pytest.mark.unit


class TestSystemPrompt:
    def test_load_system_prompt_success(self) -> None:
        with patch("gobby.prompts.loader.PromptLoader") as mock_loader:
            mock_loader.return_value.load.return_value.content = "Real prompt"
            res = _load_chat_system_prompt()
            assert res == "Real prompt"
            mock_loader.return_value.load.assert_called_once_with("chat/system")

    def test_load_system_prompt_fallback(self) -> None:
        with patch("gobby.prompts.loader.PromptLoader", side_effect=ImportError):
            res = _load_chat_system_prompt()
            assert res == _FALLBACK_SYSTEM_PROMPT


class TestFinders:
    def test_find_cli_path(self) -> None:
        with (
            patch("shutil.which", return_value="/bin/claude"),
            patch("os.path.exists", return_value=True),
            patch("os.access", return_value=True),
        ):
            assert _find_cli_path() == "/bin/claude"

    def test_find_cli_path_missing(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _find_cli_path() is None

    def test_find_project_root(self) -> None:
        _find_project_root()  # smoke test — actual root depends on CWD
        with patch("pathlib.Path.is_dir", return_value=True):
            assert _find_project_root() is not None

        with patch("pathlib.Path.is_dir", return_value=False):
            assert _find_project_root() is None

    def test_find_mcp_config(self, tmp_path) -> None:
        cwd_config = tmp_path / ".mcp.json"

        with patch("gobby.servers.chat_session_helpers.Path.cwd", return_value=tmp_path):
            assert _find_mcp_config() is None

            cwd_config.touch()
            assert _find_mcp_config() == str(cwd_config)
            cwd_config.unlink()

        with (
            patch("gobby.servers.chat_session_helpers.Path.cwd", return_value=tmp_path),
            patch("gobby.servers.chat_session_helpers._find_project_root", return_value=tmp_path),
        ):
            # Test project root fallback
            cwd_config.touch()
            assert _find_mcp_config() == str(cwd_config)

    def test_build_gobby_mcp_entry(self) -> None:
        # 1. sibling path exists
        sibling = Path(sys.executable).parent / "gobby"
        with patch("pathlib.Path.exists", return_value=True):
            res = _build_gobby_mcp_entry()
            assert res == {"command": str(sibling), "args": ["mcp-server"]}

        # 2. fallback to which
        with (
            patch("pathlib.Path.exists", return_value=False),
            patch("shutil.which", return_value="/usr/bin/gobby"),
        ):
            res = _build_gobby_mcp_entry()
            assert res == {"command": "/usr/bin/gobby", "args": ["mcp-server"]}

        # 3. fallback to bare string
        with (
            patch("pathlib.Path.exists", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            res = _build_gobby_mcp_entry()
            assert res == {"command": "gobby", "args": ["mcp-server"]}


class TestHookResponseConverters:
    def test_prompt_output(self) -> None:
        assert _response_to_prompt_output(None) == SyncHookJSONOutput()

        res = _response_to_prompt_output({"decision": "block", "reason": "No", "context": "ctx"})
        assert res["decision"] == "block"
        assert res["reason"] == "No"
        assert res["hookSpecificOutput"]["additionalContext"] == "ctx"

    def test_pre_tool_output(self) -> None:
        assert _response_to_pre_tool_output(None) == SyncHookJSONOutput()

        # Block
        res = _response_to_pre_tool_output({"decision": "block", "reason": "No"})
        assert "hookSpecificOutput" in res
        assert res["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert res["reason"] == "No"

        # Modified input
        res = _response_to_pre_tool_output(
            {"modified_input": {"a": 1}, "auto_approve": True, "context": "ctx"}
        )
        assert res["hookSpecificOutput"]["updatedInput"] == {"a": 1}
        assert res["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert res["hookSpecificOutput"]["additionalContext"] == "ctx"

        # Just context
        res = _response_to_pre_tool_output({"context": "pure ctx"})
        assert res["hookSpecificOutput"]["additionalContext"] == "pure ctx"

    def test_post_tool_output(self) -> None:
        assert _response_to_post_tool_output(None) == SyncHookJSONOutput()

        # Just context
        res = _response_to_post_tool_output({"context": "ctx"})
        assert res["hookSpecificOutput"]["additionalContext"] == "ctx"

    def test_stop_output(self) -> None:
        assert _response_to_stop_output(None) == SyncHookJSONOutput()
        res = _response_to_stop_output({"decision": "block", "context": "ctx"})
        assert res["decision"] == "block"
        assert res["hookSpecificOutput"]["additionalContext"] == "ctx"

    def test_compact_output(self) -> None:
        ctx = build_compaction_context(
            session_ref="#1", project_id="test", cwd="/tmp", source="test"
        )
        assert "Gobby Session ID: #1" in ctx
        assert "Project ID: test" in ctx

        assert _response_to_compact_output(None) == SyncHookJSONOutput()
        res = _response_to_compact_output({"context": "ctx"})
        assert res["hookSpecificOutput"]["additionalContext"] == "ctx"

    def test_subagent_output(self) -> None:
        assert _response_to_subagent_output(None, "SubagentStart") == SyncHookJSONOutput()
        res = _response_to_subagent_output({"context": "ctx"}, "SubagentStart")
        assert res["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
        assert res["hookSpecificOutput"]["additionalContext"] == "ctx"
