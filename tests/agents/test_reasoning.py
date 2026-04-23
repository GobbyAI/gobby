from unittest.mock import MagicMock

import pytest

import gobby.agents.reasoning as reasoning
from gobby.agents.reasoning import (
    normalize_reasoning_effort,
    resolve_spawn_reasoning,
)
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def test_normalize_reasoning_effort_treats_auto_as_unset() -> None:
    assert normalize_reasoning_effort("auto") is None
    assert normalize_reasoning_effort(" AUTO ") is None
    assert normalize_reasoning_effort("") is None
    assert normalize_reasoning_effort("High") == "high"


def test_resolve_spawn_reasoning_applies_supported_claude_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_resolve_spawn_reasoning_rejects_unsupported_model_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_resolve_spawn_reasoning_warns_when_provider_adapter_is_not_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_get_provider_models_reuses_fallback_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    created_for: list[object | None] = []

    class FakeCatalog:
        def __init__(self, daemon_config: object | None) -> None:
            created_for.append(daemon_config)

        def get_provider_snapshot(self, provider: str) -> dict[str, object]:
            return {"models": [{"value": f"{provider}-sonnet"}]}

    monkeypatch.setattr(reasoning, "_fallback_catalog", None)
    monkeypatch.setattr(reasoning, "_fallback_catalog_config", None)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: None)
    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    first = reasoning._get_provider_models("claude", None)
    second = reasoning._get_provider_models("claude", None)

    assert first == second == [{"value": "claude-sonnet"}]
    assert created_for == [None]


def test_get_provider_models_rebuilds_fallback_catalog_on_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_a = MagicMock(spec=DaemonConfig)
    config_b = MagicMock(spec=DaemonConfig)
    created_for: list[DaemonConfig | None] = []

    class FakeCatalog:
        def __init__(self, daemon_config: DaemonConfig | None) -> None:
            created_for.append(daemon_config)

        def get_provider_snapshot(self, provider: str) -> dict[str, object]:
            return {"models": [{"value": provider}]}

    monkeypatch.setattr(reasoning, "_fallback_catalog", None)
    monkeypatch.setattr(reasoning, "_fallback_catalog_config", None)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: None)
    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    reasoning._get_provider_models("claude", config_a)
    reasoning._get_provider_models("claude", config_b)

    assert created_for == [config_a, config_b]
