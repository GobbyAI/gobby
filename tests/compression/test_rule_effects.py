"""Integration tests for rewrite_input and compress_output rule effects."""

import pytest

from gobby.hooks.events import HookResponse
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect

pytestmark = pytest.mark.unit


class TestRewriteInputEffect:
    def test_rewrite_input_model(self) -> None:
        effect = RuleEffect(
            type="rewrite_input",
            input_updates={"command": "gobby compress -- git status"},
            auto_approve=True,
        )
        assert effect.type == "rewrite_input"
        assert effect.input_updates == {"command": "gobby compress -- git status"}
        assert effect.auto_approve is True

    def test_rewrite_input_in_rule_body(self) -> None:
        body = RuleDefinitionBody(
            event="before_tool",
            effect=RuleEffect(
                type="rewrite_input",
                input_updates={"command": "gobby compress -- git status"},
                auto_approve=True,
            ),
        )
        effects = body.resolved_effects
        assert len(effects) == 1
        assert effects[0].type == "rewrite_input"


class TestCompressOutputEffect:
    def test_compress_output_model(self) -> None:
        effect = RuleEffect(
            type="compress_output",
            strategy="pytest",
            max_lines=50,
        )
        assert effect.type == "compress_output"
        assert effect.strategy == "pytest"
        assert effect.max_lines == 50

    def test_compress_output_in_rule_body(self) -> None:
        body = RuleDefinitionBody(
            event="after_tool",
            effect=RuleEffect(
                type="compress_output",
            ),
        )
        effects = body.resolved_effects
        assert len(effects) == 1
        assert effects[0].type == "compress_output"


class TestHookResponseNewFields:
    def test_modified_input_field(self) -> None:
        resp = HookResponse(
            decision="allow",
            modified_input={"command": "gobby compress -- git status"},
            auto_approve=True,
        )
        assert resp.modified_input == {"command": "gobby compress -- git status"}
        assert resp.auto_approve is True

    def test_modified_output_field(self) -> None:
        resp = HookResponse(
            decision="allow",
            modified_output="compressed output here",
        )
        assert resp.modified_output == "compressed output here"

    def test_defaults_none(self) -> None:
        resp = HookResponse()
        assert resp.modified_input is None
        assert resp.auto_approve is False
        assert resp.modified_output is None


class TestClaudeCodeAdapterTranslation:
    def test_pre_tool_use_with_modified_input(self) -> None:
        from gobby.adapters.claude_code import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        resp = HookResponse(
            decision="allow",
            modified_input={"command": "gobby compress -- git status"},
            auto_approve=True,
        )
        result = adapter.translate_from_hook_response(resp, hook_type="pre-tool-use")
        assert result["continue"] is True
        hook_output = result.get("hookSpecificOutput", {})
        assert hook_output.get("updatedInput") == {"command": "gobby compress -- git status"}
        assert hook_output.get("permissionDecision") == "allow"

    def test_post_tool_use_with_modified_output(self) -> None:
        from gobby.adapters.claude_code import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        resp = HookResponse(
            decision="allow",
            modified_output="compressed output",
        )
        result = adapter.translate_from_hook_response(resp, hook_type="post-tool-use")
        hook_output = result.get("hookSpecificOutput", {})
        assert hook_output.get("updatedMCPToolOutput") == "compressed output"

    def test_no_modified_fields_unchanged(self) -> None:
        from gobby.adapters.claude_code import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        resp = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(resp, hook_type="pre-tool-use")
        hook_output = result.get("hookSpecificOutput")
        # No hookSpecificOutput when there's no context or modified input
        if hook_output:
            assert "updatedInput" not in hook_output


class TestSDKHelperTranslation:
    def test_pre_tool_with_modified_input(self) -> None:
        from gobby.servers.chat_session_helpers import _response_to_pre_tool_output

        resp = {
            "decision": "allow",
            "modified_input": {"command": "gobby compress -- git status"},
            "auto_approve": True,
        }
        output = _response_to_pre_tool_output(resp)
        hook_specific = output.get("hookSpecificOutput")
        assert hook_specific is not None
        assert hook_specific.get("updatedInput") == {"command": "gobby compress -- git status"}
        assert hook_specific.get("permissionDecision") == "allow"

    def test_post_tool_with_modified_output(self) -> None:
        from gobby.servers.chat_session_helpers import _response_to_post_tool_output

        resp = {
            "modified_output": "compressed output",
        }
        output = _response_to_post_tool_output(resp)
        hook_specific = output.get("hookSpecificOutput")
        assert hook_specific is not None
        assert hook_specific.get("updatedMCPToolOutput") == "compressed output"

    def test_post_tool_without_modified_output(self) -> None:
        from gobby.servers.chat_session_helpers import _response_to_post_tool_output

        resp = {"context": "some context"}
        output = _response_to_post_tool_output(resp)
        hook_specific = output.get("hookSpecificOutput")
        assert hook_specific is not None
        assert "updatedMCPToolOutput" not in hook_specific
        assert hook_specific.get("additionalContext") is not None
