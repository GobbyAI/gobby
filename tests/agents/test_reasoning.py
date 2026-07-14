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


def test_resolve_spawn_reasoning_applies_claude_opus_xhigh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.agents.reasoning._get_provider_models",
        lambda provider, daemon_config: [
            {
                "value": "opus",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh", "max"]},
            }
        ],
    )

    result = resolve_spawn_reasoning(
        provider="claude",
        model="opus",
        requested_effort="xhigh",
        reasoning_required=True,
    )

    assert result.status == "applied"
    assert result.effective_effort == "xhigh"
    assert result.reasoning_required is True
    assert result.message is None


def test_resolve_spawn_reasoning_applies_codex_gpt_55_xhigh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.agents.reasoning._get_provider_models",
        lambda provider, daemon_config: [
            {
                "value": "gpt-5.5",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
            }
        ],
    )

    result = resolve_spawn_reasoning(
        provider="codex",
        model="gpt-5.5",
        requested_effort="xhigh",
        reasoning_required=True,
    )

    assert result.status == "applied"
    assert result.effective_effort == "xhigh"
    assert result.reasoning_required is True
    assert result.message is None


def test_resolve_spawn_reasoning_rejects_qwen_terminal_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.agents.reasoning._get_provider_models",
        lambda provider, daemon_config: [
            {
                "value": "qwen3-coder",
                "reasoning": {"supported_efforts": ["low", "medium", "high"]},
            }
        ],
    )

    result = resolve_spawn_reasoning(
        provider="qwen",
        model="qwen3-coder",
        requested_effort="high",
        reasoning_required=False,
    )

    assert result.status == "unsupported_provider"
    assert result.requested_effort == "high"
    assert result.effective_effort is None
    assert result.message == (
        "Requested reasoning 'high' was not applied because spawned-terminal reasoning "
        "is not wired for provider 'qwen'."
    )


def test_resolve_spawn_reasoning_applies_droid_high() -> None:
    result = resolve_spawn_reasoning(
        provider="droid",
        model=None,
        requested_effort="high",
        reasoning_required=True,
    )

    assert result.status == "applied"
    assert result.requested_effort == "high"
    assert result.effective_effort == "high"
    assert result.reasoning_required is True
    assert result.message is None


def test_get_provider_models_reuses_fallback_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    created_for: list[object | None] = []

    class FakeCatalog:
        def __init__(self) -> None:
            created_for.append(None)

        def get_provider_snapshot(self, provider: str) -> dict[str, object]:
            return {"models": [{"value": f"{provider}-sonnet"}]}

    monkeypatch.setattr(reasoning, "_fallback_catalog", None)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: None)
    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    first = reasoning._get_provider_models("claude", None)
    second = reasoning._get_provider_models("claude", None)

    assert first == second == [{"value": "claude-sonnet"}]
    assert created_for == [None]


def test_new_fallback_catalog_uses_no_arg_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_for: list[DaemonConfig | None] = []

    class FakeCatalog:
        def __init__(self) -> None:
            created_for.append(None)

    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    assert reasoning._new_fallback_catalog().__class__ is FakeCatalog
    assert created_for == [None]


def test_new_fallback_catalog_supports_no_arg_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = 0

    class FakeCatalog:
        def __init__(self) -> None:
            nonlocal created
            created += 1

    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    assert reasoning._new_fallback_catalog().__class__ is FakeCatalog
    assert created == 1


def test_new_fallback_catalog_chains_last_constructor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCatalog:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise TypeError("constructor failed")

    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    with pytest.raises(TypeError, match="constructor failed"):
        reasoning._new_fallback_catalog()


def test_get_provider_models_reuses_fallback_catalog_for_equal_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_a = DaemonConfig()
    config_b = DaemonConfig()
    created_for: list[DaemonConfig | None] = []

    class FakeCatalog:
        def __init__(self) -> None:
            created_for.append(None)

        def get_provider_snapshot(self, provider: str) -> dict[str, object]:
            return {"models": [{"value": provider}]}

    monkeypatch.setattr(reasoning, "_fallback_catalog", None)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: None)
    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    reasoning._get_provider_models("claude", config_a)
    reasoning._get_provider_models("claude", config_b)

    assert config_a == config_b
    assert created_for == [None]


def test_get_provider_models_reuses_fallback_catalog_on_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_a = DaemonConfig(daemon_port=60887)
    config_b = DaemonConfig(daemon_port=60888)
    created_for: list[DaemonConfig | None] = []

    class FakeCatalog:
        def __init__(self) -> None:
            created_for.append(None)

        def get_provider_snapshot(self, provider: str) -> dict[str, object]:
            return {"models": [{"value": provider}]}

    monkeypatch.setattr(reasoning, "_fallback_catalog", None)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: None)
    monkeypatch.setattr("gobby.servers.provider_models.ProviderModelCatalog", FakeCatalog)

    reasoning._get_provider_models("claude", config_a)
    reasoning._get_provider_models("claude", config_b)

    assert created_for == [None]
