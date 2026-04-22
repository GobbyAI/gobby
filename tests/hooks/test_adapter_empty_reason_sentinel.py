"""Regression coverage for adapter empty-reason sentinel handling."""

from __future__ import annotations

import logging

import pytest

from gobby.adapters.base import (
    ADAPTER_EMPTY_BLOCK_REASON_SENTINEL,
    normalize_adapter_response_reason,
)
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.adapter import CodexHooksAdapter
from gobby.adapters.gemini import GeminiAdapter
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit


def _warning_messages(
    caplog: pytest.LogCaptureFixture,
    *,
    logger_name: str,
) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name and record.levelno == logging.WARNING
    ]


def test_shared_helper_warns_and_uses_sentinel_for_blank_block_reason(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    response = HookResponse(decision="block", reason="   ", context="ctx")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        reason = normalize_adapter_response_reason(
            response,
            adapter_name="TestAdapter",
            hook_type="BeforeTool",
            logger=logging.getLogger("gobby.adapters.tests"),
        )

    assert reason == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    messages = _warning_messages(caplog, logger_name="gobby.adapters.tests")
    assert any(
        "TestAdapter translated block without reason at adapter boundary" in msg for msg in messages
    )
    assert any(
        "response={'decision': 'block'" in msg and "'context': 'ctx'" in msg for msg in messages
    )


def test_shared_helper_passes_through_populated_reason_without_warning(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    response = HookResponse(decision="block", reason="Policy violation")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        reason = normalize_adapter_response_reason(
            response,
            adapter_name="TestAdapter",
            hook_type="BeforeTool",
            logger=logging.getLogger("gobby.adapters.tests"),
        )

    assert reason == "Policy violation"
    assert _warning_messages(caplog, logger_name="gobby.adapters.tests") == []


def test_claude_blank_reason_uses_sentinel_and_logs_payload(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = ClaudeCodeAdapter()
    response = HookResponse(decision="block", reason="  ", context="ctx")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    )
    messages = _warning_messages(caplog, logger_name="gobby.adapters.claude_code")
    assert any(
        "ClaudeCodeAdapter translated block without reason at adapter boundary" in msg
        for msg in messages
    )
    assert any(
        "response={'decision': 'block'" in msg and "'context': 'ctx'" in msg for msg in messages
    )


def test_claude_populated_reason_passes_through_unchanged(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = ClaudeCodeAdapter()
    response = HookResponse(decision="block", reason="Blocked by policy")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "Blocked by policy"
    assert _warning_messages(caplog, logger_name="gobby.adapters.claude_code") == []


def test_gemini_blank_reason_uses_sentinel_and_logs_payload(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = GeminiAdapter()
    response = HookResponse(decision="block", reason="", context="ctx")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="BeforeTool")

    assert result["reason"] == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    messages = _warning_messages(caplog, logger_name="gobby.adapters.gemini")
    assert any(
        "GeminiAdapter translated block without reason at adapter boundary" in msg
        for msg in messages
    )
    assert any(
        "response={'decision': 'block'" in msg and "'context': 'ctx'" in msg for msg in messages
    )


def test_gemini_populated_reason_passes_through_unchanged(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = GeminiAdapter()
    response = HookResponse(decision="block", reason="Blocked by workflow")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="BeforeTool")

    assert result["reason"] == "Blocked by workflow"
    assert _warning_messages(caplog, logger_name="gobby.adapters.gemini") == []


def test_codex_blank_reason_uses_sentinel_and_logs_payload(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(decision="block", reason="", context="ctx")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    assert result["reason"] == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
    )
    messages = _warning_messages(caplog, logger_name="gobby.adapters.codex_impl.adapter")
    assert any(
        "CodexHooksAdapter translated block without reason at adapter boundary" in msg
        for msg in messages
    )
    assert any(
        "response={'decision': 'block'" in msg and "'context': 'ctx'" in msg for msg in messages
    )


def test_codex_populated_reason_passes_through_unchanged(
    enable_log_propagation, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(decision="block", reason="Blocked by rule")

    with caplog.at_level(logging.WARNING, logger="gobby"):
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    assert result["reason"] == "Blocked by rule"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "Blocked by rule"
    assert _warning_messages(caplog, logger_name="gobby.adapters.codex_impl.adapter") == []
