"""End-to-end extensibility proof for provider capability adapters."""

from datetime import UTC, datetime

import pytest

from gobby.providers.capabilities.activation import (
    ActivationHandler,
    register_activation_handler,
)
from gobby.providers.capabilities.collectors import (
    SourceSpec,
    collectors,
    register_collector,
    validate_snapshot,
)
from gobby.providers.capabilities.models import (
    ActivationDescriptor,
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

_PROVIDER = "fake-provider-19624"
_ACTIVATION_KIND = "fake_selector_19624"
_SOURCE_URL = "https://fake-provider.test/models"
_OBSERVED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _provenance(*facts: str) -> dict[str, FactProvenance]:
    provenance = FactProvenance(
        source_key="fake-api",
        source_url=_SOURCE_URL,
        observed_at=_OBSERVED_AT,
    )
    return dict.fromkeys(facts, provenance)


class _FakeCollector:
    provider = _PROVIDER
    sources: tuple[SourceSpec, ...] = (
        SourceSpec(source_key="fake-api", url=_SOURCE_URL, required=True),
    )

    async def collect(self) -> ProviderSnapshot:
        activation = ActivationDescriptor(
            kind=_ACTIVATION_KIND,
            surface="tool-chat",
            params={"name": "fake-model"},
        )
        route = ModelRoute(
            speed_mode=SpeedMode.STANDARD,
            selector="fake-model",
            available=True,
            usage_multiplier=None,
            throughput_multiplier=None,
            latency_class=None,
            activations=(activation,),
            provenance=_provenance("speed_mode", "selector", "available", "activations"),
        )
        model = ModelCapability(
            canonical_model="fake-model",
            display_name="Fake Model",
            aliases=(),
            available=True,
            hidden=False,
            is_default=True,
            context_length=None,
            max_output_tokens=None,
            reasoning=ReasoningSupport.UNKNOWN,
            supported_efforts=None,
            default_effort=None,
            latency_class=None,
            input_modalities=None,
            supports_tools=None,
            routes=(route,),
            provenance=_provenance(
                "canonical_model",
                "display_name",
                "aliases",
                "available",
                "hidden",
                "is_default",
                "reasoning",
            ),
        )
        source = SourceHealth(
            source_key="fake-api",
            source_url=_SOURCE_URL,
            required=True,
            state=SourceState.OK,
            attempts=1,
            last_attempt_at=_OBSERVED_AT,
            last_success_at=_OBSERVED_AT,
            last_error=None,
        )
        return ProviderSnapshot(
            provider=self.provider,
            generation=0,
            models=(model,),
            sources=(source,),
        )


@pytest.mark.asyncio
async def test_fake_provider_end_to_end(postgres_db: HubDatabase) -> None:
    register_activation_handler(
        _ACTIVATION_KIND,
        ActivationHandler(
            surfaces=frozenset({"tool-chat"}),
            allowed_params=frozenset({"name"}),
        ),
    )
    collector = _FakeCollector()
    register_collector(collector)

    snapshot = validate_snapshot(await collectors()[_PROVIDER].collect(), collector.sources)
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(snapshot)

    stored = store.get_provider_snapshot(_PROVIDER)
    assert stored is not None
    assert stored.models[0].routes[0].activations[0].kind == _ACTIVATION_KIND
