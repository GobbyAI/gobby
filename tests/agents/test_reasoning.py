from unittest.mock import Mock

import pytest

import gobby.agents.reasoning as reasoning
from gobby.agents.reasoning import normalize_reasoning_effort, resolve_spawn_reasoning
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.resolve import (
    CapabilityResolver,
    ReasoningResolution,
    ReasoningStatus,
)

pytestmark = pytest.mark.unit


class _Store:
    def __init__(self, snapshot: ProviderSnapshot | None) -> None:
        self.snapshot = snapshot

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        if self.snapshot is None or self.snapshot.provider != provider:
            return None
        return self.snapshot


class _MetadataStore:
    def get_context_window(self, model: str) -> None:
        return None

    def get_model_metadata(self, model: str) -> None:
        return None


def _resolver(
    provider: str,
    model: str,
    *,
    support: ReasoningSupport = ReasoningSupport.KNOWN,
    efforts: tuple[str, ...] | None = ("low", "medium", "high"),
    default_effort: str | None = None,
) -> CapabilityResolver:
    capability = ModelCapability(
        canonical_model=model,
        display_name=model,
        aliases=(),
        available=True,
        hidden=False,
        is_default=True,
        context_length=None,
        max_output_tokens=None,
        reasoning=support,
        supported_efforts=efforts,
        default_effort=default_effort,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        routes=(),
        provenance={},
    )
    snapshot = ProviderSnapshot(provider=provider, generation=1, models=(capability,), sources=())
    return CapabilityResolver(_Store(snapshot), _MetadataStore())


def test_normalize_reasoning_effort_preserves_auto() -> None:
    assert normalize_reasoning_effort("auto") == "auto"
    assert normalize_reasoning_effort(" AUTO ") == "auto"
    assert normalize_reasoning_effort("") is None
    assert normalize_reasoning_effort("High") == "high"


def test_unknown_model_passes_through_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = Mock(spec=CapabilityResolver)
    resolver.resolve_reasoning.return_value = ReasoningResolution(
        requested_effort="high",
        effective_effort="high",
        status=ReasoningStatus.UNVERIFIED,
        reason=None,
    )
    monkeypatch.setattr(reasoning, "_get_capability_resolver", lambda: resolver)

    result = resolve_spawn_reasoning(
        provider="claude",
        model="future-claude-model",
        requested_effort="high",
        reasoning_required=True,
    )

    assert result.status == "unverified"
    assert result.effective_effort == "high"
    assert result.reasoning_required is True
    resolver.resolve_reasoning.assert_called_once_with(
        "claude",
        "future-claude-model",
        "high",
        transport_supports_effort=True,
    )


def test_supported_model_effort_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reasoning,
        "_get_capability_resolver",
        lambda: _resolver("claude", "claude-sonnet-5"),
    )

    result = resolve_spawn_reasoning(
        provider="claude",
        model="claude-sonnet-5",
        requested_effort="high",
        reasoning_required=False,
    )

    assert result.status == "applied"
    assert result.effective_effort == "high"


def test_unsupported_model_effort_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reasoning,
        "_get_capability_resolver",
        lambda: _resolver("claude", "claude-sonnet-5"),
    )

    result = resolve_spawn_reasoning(
        provider="claude",
        model="claude-sonnet-5",
        requested_effort="xhigh",
        reasoning_required=True,
    )

    assert result.status == "unsupported_model"
    assert result.effective_effort is None
    assert result.reasoning_required is True


def test_transport_without_reasoning_flag_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reasoning,
        "_get_capability_resolver",
        lambda: _resolver("qwen", "qwen3-coder"),
    )

    result = resolve_spawn_reasoning(
        provider="qwen",
        model="qwen3-coder",
        requested_effort="high",
        reasoning_required=False,
    )

    assert result.status == "unsupported_provider"
    assert result.effective_effort is None


def test_auto_without_metadata_is_preserved_as_unverified() -> None:
    result = resolve_spawn_reasoning(
        provider="droid",
        model="gpt-5.4",
        requested_effort="auto",
        reasoning_required=True,
    )

    assert result.requested_effort == "auto"
    assert result.status == "unverified"
    assert result.effective_effort is None
    assert result.reasoning_required is True


def test_spawn_auto_persists_concrete_native_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reasoning,
        "_get_capability_resolver",
        lambda: _resolver("codex", "gpt-5.6-luna", default_effort="medium"),
    )

    result = resolve_spawn_reasoning(
        provider="codex",
        model="gpt-5.6-luna",
        requested_effort="auto",
        reasoning_required=False,
    )

    assert result.requested_effort == "auto"
    assert result.effective_effort == "medium"
    assert result.status == "applied"


def test_none_reasoning_request_skips_resolution() -> None:
    result = resolve_spawn_reasoning(
        provider="droid",
        model="gpt-5.4",
        requested_effort=None,
        reasoning_required=True,
    )

    assert result.status == "not_requested"
    assert result.requested_effort is None
    assert result.effective_effort is None
    assert result.reasoning_required is False
