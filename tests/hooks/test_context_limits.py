"""Tests for configurable hook additionalContext limits."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.hooks.context_limits import (
    additional_context_limit_for,
    handoff_summary_inject_budget_for,
    inline_context_budget_for,
)
from gobby.hooks.events import SessionSource
from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    HANDOFF_SUMMARY_INJECT_BUDGET,
    truncate_additional_context,
)

pytestmark = pytest.mark.unit


def test_defaults_match_claude_sdk_ceiling() -> None:
    assert additional_context_limit_for(SessionSource.GROK) == ADDITIONAL_CONTEXT_LIMIT
    assert inline_context_budget_for(SessionSource.GROK) == 9_500
    assert handoff_summary_inject_budget_for(SessionSource.GROK) == HANDOFF_SUMMARY_INJECT_BUDGET


def test_provider_override_from_active_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks import context_limits

    monkeypatch.setattr(
        context_limits,
        "_active_hooks_config",
        lambda: SimpleNamespace(
            additional_context_limit=9_950,
            additional_context_limits={"grok": 20_000},
        ),
    )

    assert additional_context_limit_for(SessionSource.GROK) == 20_000
    assert additional_context_limit_for(SessionSource.CLAUDE) == 9_950
    assert additional_context_limit_for("GROK") == 20_000
    assert inline_context_budget_for(SessionSource.GROK) == 19_550
    assert handoff_summary_inject_budget_for(SessionSource.GROK) == 14_550


def test_daemon_config_accepts_provider_overrides() -> None:
    config = DaemonConfig(
        hooks={
            "additional_context_limit": 9_950,
            "additional_context_limits": {"Grok": 16_000},
        }
    )
    assert config.hooks.additional_context_limits == {"grok": 16_000}


def test_daemon_config_rejects_tiny_provider_limit() -> None:
    with pytest.raises(ValidationError, match="256"):
        DaemonConfig(hooks={"additional_context_limits": {"grok": 10}})


def test_daemon_config_rejects_colliding_provider_keys() -> None:
    with pytest.raises(ValidationError, match="duplicate provider key"):
        DaemonConfig(hooks={"additional_context_limits": {"Grok": 16_000, "grok": 20_000}})


def test_adapter_bounding_keeps_telemetry_and_does_not_prefix_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from gobby.adapters.capabilities import ContextChannel
    from gobby.adapters.degradation import AdapterDegradationKind, truncate_context_for_adapter

    unique_head = "UNIQUE_ADAPTER_HEAD_7f3a9c"
    text = unique_head + "z" * 600
    monkeypatch.setattr(
        "gobby.adapters.degradation.additional_context_limit_for",
        lambda _provider: 80,
    )
    with patch("gobby.adapters.degradation.record_adapter_degradation") as record:
        result = truncate_context_for_adapter(
            text,
            provider=SessionSource.CLAUDE,
            hook_type="SessionStart",
            destination_channel=ContextChannel.ADDITIONAL_CONTEXT,
        )
    record.assert_called_once()
    assert record.call_args.kwargs["kind"] is AdapterDegradationKind.CONTEXT_TRUNCATED
    assert unique_head not in result
    assert "omitted contributors=" in result
    assert len(result) <= 80


def test_oversized_handoff_summary_is_pointer_only() -> None:
    from types import SimpleNamespace

    from gobby.hooks.event_handlers._session_start.handoff import _bound_handoff_summary

    unique_head = "UNIQUE_HANDOFF_HEAD_7f3a9c"
    summary = unique_head + "h" * (HANDOFF_SUMMARY_INJECT_BUDGET + 200)
    result = _bound_handoff_summary(summary, SimpleNamespace(seq_num=7, source="claude"))
    assert unique_head not in result
    assert "get_handoff_context" in result
    assert "#7" in result
    assert len(result) <= HANDOFF_SUMMARY_INJECT_BUDGET


def test_truncate_honors_explicit_limit() -> None:
    unique_head = "UNIQUE_LIMIT_HEAD_7f3a9c"
    text = unique_head + "x" * 600
    result = truncate_additional_context(text, limit=80)
    assert len(result) <= 80
    assert unique_head not in result
    assert "omitted contributors=" in result


def test_adapter_bounding_forwards_persist_kwargs() -> None:
    from unittest.mock import MagicMock

    from gobby.adapters.capabilities import ContextChannel
    from gobby.adapters.degradation import truncate_context_for_adapter

    store = MagicMock()
    store.save.return_value = "result-adapter-ac-1"
    unique_head = "UNIQUE_ADAPTER_PERSIST_7f3a9c"
    text = unique_head + "z" * (ADDITIONAL_CONTEXT_LIMIT + 50)
    result = truncate_context_for_adapter(
        text,
        provider=SessionSource.CLAUDE,
        hook_type="SessionStart",
        destination_channel=ContextChannel.ADDITIONAL_CONTEXT,
        session_id="sess-1",
        project_id="proj-1",
        store=store,
    )
    store.save.assert_called_once()
    assert store.save.call_args.kwargs["content"] == text
    assert unique_head not in result
    assert "get_tool_result result_id=result-adapter-ac-1" in result


def test_persist_kwargs_from_hook_response_use_manager_database() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from gobby.adapters.degradation import persist_kwargs_from_hook_response
    from gobby.hooks.events import HookResponse

    store = MagicMock()
    response = HookResponse(
        decision="allow",
        metadata={"session_id": "sess-1", "project_id": "proj-1"},
    )
    hook_manager = SimpleNamespace(_database=object())
    with patch("gobby.adapters.degradation.ToolResultStore", return_value=store) as store_cls:
        kwargs = persist_kwargs_from_hook_response(response, hook_manager)
    store_cls.assert_called_once()
    assert store_cls.call_args.args[0] is hook_manager._database
    assert kwargs["session_id"] == "sess-1"
    assert kwargs["project_id"] == "proj-1"
    assert kwargs["store"] is store


@pytest.mark.parametrize(
    ("adapter_factory", "hook_type"),
    [
        ("codex", "UserPromptSubmit"),
        ("droid", "UserPromptSubmit"),
        ("claude", "user-prompt-submit"),
        ("grok", "SessionStart"),
    ],
)
def test_production_adapters_persist_overflow(adapter_factory: str, hook_type: str) -> None:
    from types import SimpleNamespace
    from typing import Any
    from unittest.mock import MagicMock, patch

    from gobby.adapters.claude_code import ClaudeCodeAdapter
    from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
    from gobby.adapters.droid import DroidAdapter
    from gobby.adapters.grok import GrokAdapter
    from gobby.hooks.events import HookResponse

    adapters: dict[str, type[Any]] = {
        "codex": CodexHooksAdapter,
        "droid": DroidAdapter,
        "claude": ClaudeCodeAdapter,
        "grok": GrokAdapter,
    }
    store = MagicMock()
    store.save.return_value = f"result-{adapter_factory}-1"
    unique_head = f"UNIQUE_{adapter_factory.upper()}_SHIP_7f3a9c"
    full = unique_head + "x" * (ADDITIONAL_CONTEXT_LIMIT + 3_000)
    adapter = adapters[adapter_factory]()
    adapter._hook_manager = SimpleNamespace(_database=object())
    response = HookResponse(
        decision="allow",
        context=full,
        metadata={"session_id": "sess-1", "project_id": "proj-1"},
    )
    with patch("gobby.adapters.degradation.ToolResultStore", return_value=store):
        result = adapter.translate_from_hook_response(response, hook_type=hook_type)
    store.save.assert_called_once()
    assert unique_head in store.save.call_args.kwargs["content"]
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert unique_head not in ctx
    assert f"get_tool_result result_id=result-{adapter_factory}-1" in ctx
    assert "... [truncated]" not in ctx


def test_grok_subagent_stop_persists_overflow() -> None:
    from types import SimpleNamespace
    from typing import cast
    from unittest.mock import MagicMock, patch

    from gobby.adapters.grok import GrokAdapter
    from gobby.hooks.events import HookResponse
    from gobby.hooks.hook_manager import HookManager

    store = MagicMock()
    store.save.return_value = "result-grok-stop-1"
    unique_head = "UNIQUE_GROK_STOP_7f3a9c"
    full = unique_head + "x" * (ADDITIONAL_CONTEXT_LIMIT + 3_000)
    adapter = GrokAdapter()
    adapter._hook_manager = cast(HookManager, SimpleNamespace(_database=object()))
    response = HookResponse(
        decision="block",
        context=full,
        metadata={"session_id": "sess-1", "project_id": "proj-1"},
    )
    with patch("gobby.adapters.degradation.ToolResultStore", return_value=store):
        result = adapter.translate_from_hook_response(response, hook_type="subagent_stop")
    store.save.assert_called_once()
    assert unique_head in store.save.call_args.kwargs["content"]
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert unique_head not in ctx
    assert "get_tool_result result_id=result-grok-stop-1" in ctx
