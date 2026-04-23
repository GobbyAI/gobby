"""Focused adapter tests for empty block/deny reason sentinel handling."""

from __future__ import annotations

import logging

import pytest

from gobby.adapters.base import (
    ADAPTER_EMPTY_BLOCK_REASON_SENTINEL,
    normalize_adapter_response_reason,
)
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.gemini import GeminiAdapter
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit


def test_shared_helper_warns_and_uses_sentinel_for_blank_block_reason(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    response = HookResponse(decision="block", reason="   ")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        reason = normalize_adapter_response_reason(
            response,
            adapter_name="TestAdapter",
            hook_type="BeforeTool",
            logger=logging.getLogger("gobby.adapters.tests"),
        )

    assert reason == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    assert any(
        record.levelno == logging.WARNING
        and "TestAdapter translated block without reason at adapter boundary" in record.message
        for record in caplog.records
    )


def test_claude_pre_tool_use_blank_reason_uses_sentinel(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = ClaudeCodeAdapter()
    response = HookResponse(decision="deny", reason="   ")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

    assert result["continue"] is True
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    )
    assert any(
        record.name == "gobby.adapters.claude_code"
        and "ClaudeCodeAdapter translated deny without reason at adapter boundary" in record.message
        for record in caplog.records
    )


def test_gemini_block_blank_reason_uses_sentinel(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = GeminiAdapter()
    response = HookResponse(decision="block", reason="")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="BeforeTool")

    assert result["decision"] == "block"
    assert result["continue"] is True
    assert result["reason"] == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    assert any(
        record.name == "gobby.adapters.gemini"
        and "GeminiAdapter translated block without reason at adapter boundary" in record.message
        for record in caplog.records
    )


def test_codex_pre_tool_use_blank_reason_uses_sentinel(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(decision="block", reason="")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    assert result["decision"] == "block"
    assert result["reason"] == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    )
    assert any(
        record.name == "gobby.adapters.codex_impl.hooks_adapter"
        and "CodexHooksAdapter translated block without reason at adapter boundary"
        in record.message
        for record in caplog.records
    )
