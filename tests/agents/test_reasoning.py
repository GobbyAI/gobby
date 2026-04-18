import pytest

from gobby.agents.reasoning import (
    normalize_reasoning_effort,
    resolve_spawn_reasoning,
)

pytestmark = pytest.mark.unit


def test_normalize_reasoning_effort_treats_auto_as_unset() -> None:
    assert normalize_reasoning_effort("auto") is None
    assert normalize_reasoning_effort(" AUTO ") is None
    assert normalize_reasoning_effort("") is None
    assert normalize_reasoning_effort("High") == "high"


def test_resolve_spawn_reasoning_applies_supported_claude_effort(monkeypatch) -> None:
    monkeypatch.setattr(
        "gobby.agents.reasoning._get_provider_models",
        lambda provider, daemon_config: [
            {
                "value": "claude-sonnet-4",
                "reasoning": {"supported_efforts": ["low", "medium", "high"]},
            }
        ],
    )

    result = resolve_spawn_reasoning(
        provider="claude",
        model="claude-sonnet-4",
        requested_effort="high",
        reasoning_required=False,
    )

    assert result.status == "applied"
    assert result.requested_effort == "high"
    assert result.effective_effort == "high"
    assert result.message is None


def test_resolve_spawn_reasoning_rejects_unsupported_model_effort(monkeypatch) -> None:
    monkeypatch.setattr(
        "gobby.agents.reasoning._get_provider_models",
        lambda provider, daemon_config: [
            {
                "value": "claude-sonnet-4",
                "reasoning": {"supported_efforts": ["low", "medium", "high"]},
            }
        ],
    )

    result = resolve_spawn_reasoning(
        provider="claude",
        model="claude-sonnet-4",
        requested_effort="xhigh",
        reasoning_required=True,
    )

    assert result.status == "unsupported_model"
    assert result.requested_effort == "xhigh"
    assert result.effective_effort is None
    assert result.reasoning_required is True
    assert result.message is not None


def test_resolve_spawn_reasoning_warns_when_provider_adapter_is_not_wired(monkeypatch) -> None:
    monkeypatch.setattr(
        "gobby.agents.reasoning._get_provider_models",
        lambda provider, daemon_config: [
            {
                "value": "gemini-2.5-pro",
                "reasoning": {"supported_efforts": ["low", "medium", "high"]},
            }
        ],
    )

    result = resolve_spawn_reasoning(
        provider="gemini",
        model="gemini-2.5-pro",
        requested_effort="high",
        reasoning_required=False,
    )

    assert result.status == "unsupported_provider"
    assert result.requested_effort == "high"
    assert result.effective_effort is None
    assert result.message is not None
