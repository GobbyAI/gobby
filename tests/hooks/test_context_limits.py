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


def test_truncate_honors_explicit_limit() -> None:
    text = "x" * 600
    result = truncate_additional_context(text, limit=500)
    assert len(result) == 500
    assert result.endswith("\n... [truncated]")
