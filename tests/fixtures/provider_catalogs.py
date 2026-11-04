"""Answered provider catalogs for spawn tests whose outcome depends on which provider serves a model.

A provider whose catalog answered is authoritative: a model it does not list is refused on
a launch that names it. Without a catalog the compatibility check cannot tell, and it lets
the model through, so tests that expect a refusal must serve the catalogs explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from gobby.agents import reasoning
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.resolve import CapabilityResolver


class _Store:
    def __init__(self, snapshots: Mapping[str, ProviderSnapshot]) -> None:
        self._snapshots = snapshots

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        return self._snapshots.get(provider)


class _NoMetadata:
    def get_context_window(self, model: str) -> None:
        return None

    def get_model_metadata(self, model: str) -> None:
        return None


def _capability(name: str) -> ModelCapability:
    return ModelCapability(
        canonical_model=name,
        display_name=name,
        aliases=(),
        available=True,
        hidden=False,
        is_default=False,
        context_length=None,
        max_output_tokens=None,
        reasoning=ReasoningSupport.KNOWN,
        supported_efforts=("low", "medium", "high", "xhigh"),
        default_effort=None,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        provenance={},
    )


def serve_catalogs(monkeypatch: pytest.MonkeyPatch, served: Mapping[str, tuple[str, ...]]) -> None:
    """Answer each listed provider's catalog with exactly the models it lists.

    A provider not listed has no catalog, so it neither serves nor authoritatively refuses.
    """
    snapshots = {
        provider: ProviderSnapshot(
            provider=provider,
            generation=1,
            models=tuple(_capability(name) for name in models),
            sources=(),
        )
        for provider, models in served.items()
    }
    resolver = CapabilityResolver(_Store(snapshots), _NoMetadata())
    monkeypatch.setattr(reasoning, "_get_capability_resolver", lambda: resolver)
