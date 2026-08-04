"""PostgreSQL persistence for provider capability snapshots."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

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
from gobby.providers.registry import provider_metadata
from gobby.storage.hub.protocol import HubDatabase, Row, Transaction

_GENERATION_SQL = """
SELECT GREATEST(
    COALESCE(
        (SELECT MAX(generation) FROM provider_model_capabilities WHERE provider = %s),
        0
    ),
    COALESCE(
        (SELECT MAX(generation) FROM provider_capability_refresh_state WHERE provider = %s),
        0
    )
) AS generation
"""

_INSERT_CAPABILITY_SQL = """
INSERT INTO provider_model_capabilities (
    provider,
    canonical_model,
    display_name,
    aliases,
    available,
    hidden,
    is_default,
    context_length,
    max_output_tokens,
    reasoning,
    supported_efforts,
    default_effort,
    latency_class,
    input_modalities,
    supports_tools,
    generation,
    provenance
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_ROUTE_SQL = """
INSERT INTO provider_model_routes (
    provider,
    canonical_model,
    speed_mode,
    selector,
    available,
    usage_multiplier,
    throughput_multiplier,
    latency_class,
    activations,
    generation,
    provenance
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_UPSERT_SOURCE_SQL = """
INSERT INTO provider_capability_refresh_state (
    provider,
    source_key,
    source_url,
    required,
    generation,
    state,
    attempts,
    last_attempt_at,
    last_success_at,
    last_error
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (provider, source_key) DO UPDATE SET
    source_url = EXCLUDED.source_url,
    required = EXCLUDED.required,
    generation = EXCLUDED.generation,
    state = EXCLUDED.state,
    attempts = EXCLUDED.attempts,
    last_attempt_at = EXCLUDED.last_attempt_at,
    last_success_at = EXCLUDED.last_success_at,
    last_error = EXCLUDED.last_error
"""

_SELECT_CAPABILITIES_SQL = """
SELECT
    provider,
    canonical_model,
    display_name,
    aliases,
    available,
    hidden,
    is_default,
    context_length,
    max_output_tokens,
    reasoning,
    supported_efforts,
    default_effort,
    latency_class,
    input_modalities,
    supports_tools,
    generation,
    provenance
FROM provider_model_capabilities
WHERE (%s::text IS NULL OR provider = %s)
ORDER BY provider, is_default DESC, display_name, canonical_model
"""

_SELECT_ROUTES_SQL = """
SELECT
    provider,
    canonical_model,
    speed_mode,
    selector,
    available,
    usage_multiplier,
    throughput_multiplier,
    latency_class,
    activations,
    generation,
    provenance
FROM provider_model_routes
WHERE (%s::text IS NULL OR provider = %s)
ORDER BY
    provider,
    canonical_model,
    CASE speed_mode WHEN 'standard' THEN 0 WHEN 'fast' THEN 1 ELSE 2 END,
    speed_mode
"""

_SELECT_SOURCES_SQL = """
SELECT
    provider,
    source_key,
    source_url,
    required,
    generation,
    state,
    attempts,
    last_attempt_at,
    last_success_at,
    last_error
FROM provider_capability_refresh_state
WHERE (%s::text IS NULL OR provider = %s)
ORDER BY provider, source_key
"""


class ProviderCapabilityStore:
    """Own durable provider snapshots and source refresh health."""

    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def replace_provider_snapshot(self, snapshot: ProviderSnapshot) -> None:
        """Atomically replace one provider's model rows with a new generation."""
        provider = snapshot.provider
        with self._db.transaction() as transaction:
            self._lock_provider(transaction, provider)
            generation = self._current_generation(transaction, provider) + 1
            transaction.execute(
                "DELETE FROM provider_model_capabilities WHERE provider = %s",
                (provider,),
            )

            capability_rows: list[Sequence[Any]] = []
            route_rows: list[Sequence[Any]] = []
            for model in snapshot.models:
                capability_rows.append(self._capability_row(provider, generation, model))
                route_rows.extend(self._route_rows(provider, generation, model))

            if capability_rows:
                transaction.executemany(_INSERT_CAPABILITY_SQL, capability_rows)
            if route_rows:
                transaction.executemany(_INSERT_ROUTE_SQL, route_rows)

            source_rows = [
                (
                    provider,
                    source.source_key,
                    source.source_url,
                    source.required,
                    generation,
                    source.state.value,
                    source.attempts,
                    source.last_attempt_at,
                    source.last_success_at,
                    source.last_error,
                )
                for source in snapshot.sources
            ]
            if source_rows:
                transaction.executemany(_UPSERT_SOURCE_SQL, source_rows)

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        """Load one provider's last durable snapshot and current source health."""
        with self._db.transaction() as transaction:
            snapshots = self._load_snapshots(transaction, provider)
        return snapshots[0] if snapshots else None

    def get_all_snapshots(self) -> tuple[ProviderSnapshot, ...]:
        """Load all provider snapshots in canonical provider display order."""
        with self._db.transaction() as transaction:
            snapshots = self._load_snapshots(transaction, None)
        display_order = {
            metadata.provider: index for index, metadata in enumerate(provider_metadata())
        }
        return tuple(
            sorted(
                snapshots,
                key=lambda snapshot: (
                    display_order.get(snapshot.provider, len(display_order)),
                    snapshot.provider,
                ),
            )
        )

    def record_source_failure(self, provider: str, source_key: str, error: str) -> None:
        """Record a failed source attempt without replacing last-good model rows."""
        with self._db.transaction() as transaction:
            self._lock_provider(transaction, provider)
            row = transaction.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM provider_model_capabilities WHERE provider = %s
                ) AS has_rows
                """,
                (provider,),
            ).fetchone()
            has_rows = bool(row and row["has_rows"])
            state = SourceState.STALE if has_rows else SourceState.ERROR
            generation = self._current_generation(transaction, provider)
            transaction.execute(
                """
                INSERT INTO provider_capability_refresh_state (
                    provider,
                    source_key,
                    generation,
                    state,
                    attempts,
                    last_attempt_at,
                    last_error
                ) VALUES (%s, %s, %s, %s, 1, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (provider, source_key) DO UPDATE SET
                    generation = GREATEST(
                        provider_capability_refresh_state.generation,
                        EXCLUDED.generation
                    ),
                    state = EXCLUDED.state,
                    attempts = provider_capability_refresh_state.attempts + 1,
                    last_attempt_at = EXCLUDED.last_attempt_at,
                    last_error = EXCLUDED.last_error
                """,
                (provider, source_key, generation, state.value, error),
            )

    def mark_stale(self, provider: str) -> None:
        """Mark every known source for a provider stale without deleting rows."""
        with self._db.transaction() as transaction:
            self._lock_provider(transaction, provider)
            transaction.execute(
                """
                UPDATE provider_capability_refresh_state
                SET state = %s
                WHERE provider = %s
                """,
                (SourceState.STALE.value, provider),
            )

    def has_rows(self, provider: str) -> bool:
        """Return whether a provider has at least one durable capability row."""
        row = self._db.fetchone(
            """
            SELECT EXISTS(
                SELECT 1 FROM provider_model_capabilities WHERE provider = %s
            ) AS has_rows
            """,
            (provider,),
        )
        return bool(row and row["has_rows"])

    @staticmethod
    def _lock_provider(transaction: Transaction, provider: str) -> None:
        transaction.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"gobby:provider-capability:{provider}",),
        )

    @staticmethod
    def _current_generation(transaction: Transaction, provider: str) -> int:
        row = transaction.execute(_GENERATION_SQL, (provider, provider)).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to read capability generation for provider {provider!r}")
        return int(row["generation"])

    @staticmethod
    def _capability_row(
        provider: str,
        generation: int,
        model: ModelCapability,
    ) -> Sequence[Any]:
        return (
            provider,
            model.canonical_model,
            model.display_name,
            Jsonb(list(model.aliases)),
            model.available,
            model.hidden,
            model.is_default,
            model.context_length,
            model.max_output_tokens,
            model.reasoning.value,
            Jsonb(list(model.supported_efforts)) if model.supported_efforts is not None else None,
            model.default_effort,
            model.latency_class,
            Jsonb(list(model.input_modalities)) if model.input_modalities is not None else None,
            model.supports_tools,
            generation,
            Jsonb({name: value.to_dict() for name, value in model.provenance.items()}),
        )

    @staticmethod
    def _route_rows(
        provider: str,
        generation: int,
        model: ModelCapability,
    ) -> list[Sequence[Any]]:
        return [
            (
                provider,
                model.canonical_model,
                route.speed_mode.value,
                route.selector,
                route.available,
                route.usage_multiplier,
                route.throughput_multiplier,
                route.latency_class,
                Jsonb([activation.to_dict() for activation in route.activations]),
                generation,
                Jsonb({name: value.to_dict() for name, value in route.provenance.items()}),
            )
            for route in model.routes
        ]

    @staticmethod
    def _load_snapshots(
        transaction: Transaction,
        provider: str | None,
    ) -> tuple[ProviderSnapshot, ...]:
        params = (provider, provider)
        capability_rows = transaction.execute(_SELECT_CAPABILITIES_SQL, params).fetchall()
        route_rows = transaction.execute(_SELECT_ROUTES_SQL, params).fetchall()
        source_rows = transaction.execute(_SELECT_SOURCES_SQL, params).fetchall()

        routes_by_model: dict[tuple[str, str], list[ModelRoute]] = defaultdict(list)
        for row in route_rows:
            routes_by_model[(str(row["provider"]), str(row["canonical_model"]))].append(
                _model_route(row)
            )

        models_by_provider: dict[str, list[ModelCapability]] = defaultdict(list)
        generations: dict[str, int] = {}
        for row in capability_rows:
            row_provider = str(row["provider"])
            canonical_model = str(row["canonical_model"])
            models_by_provider[row_provider].append(
                _model_capability(row, routes_by_model[(row_provider, canonical_model)])
            )
            generations[row_provider] = max(
                generations.get(row_provider, 0),
                int(row["generation"]),
            )

        sources_by_provider: dict[str, list[SourceHealth]] = defaultdict(list)
        for row in source_rows:
            row_provider = str(row["provider"])
            sources_by_provider[row_provider].append(_source_health(row))
            generations[row_provider] = max(
                generations.get(row_provider, 0),
                int(row["generation"]),
            )

        providers = models_by_provider.keys() | sources_by_provider.keys()
        return tuple(
            ProviderSnapshot(
                provider=row_provider,
                generation=generations[row_provider],
                models=tuple(models_by_provider[row_provider]),
                sources=tuple(sources_by_provider[row_provider]),
            )
            for row_provider in sorted(providers)
        )


def _model_capability(row: Row, routes: Sequence[ModelRoute]) -> ModelCapability:
    return ModelCapability(
        canonical_model=str(row["canonical_model"]),
        display_name=str(row["display_name"]),
        aliases=_string_tuple(row["aliases"]) or (),
        available=bool(row["available"]),
        hidden=bool(row["hidden"]),
        is_default=bool(row["is_default"]),
        context_length=_optional_int(row["context_length"]),
        max_output_tokens=_optional_int(row["max_output_tokens"]),
        reasoning=ReasoningSupport(str(row["reasoning"])),
        supported_efforts=_string_tuple(row["supported_efforts"]),
        default_effort=_optional_str(row["default_effort"]),
        latency_class=_optional_str(row["latency_class"]),
        input_modalities=_string_tuple(row["input_modalities"]),
        supports_tools=_optional_bool(row["supports_tools"]),
        routes=tuple(routes),
        provenance=_provenance(row["provenance"]),
    )


def _model_route(row: Row) -> ModelRoute:
    return ModelRoute(
        speed_mode=SpeedMode(str(row["speed_mode"])),
        selector=str(row["selector"]),
        available=bool(row["available"]),
        usage_multiplier=_optional_decimal(row["usage_multiplier"]),
        throughput_multiplier=_optional_decimal(row["throughput_multiplier"]),
        latency_class=_optional_str(row["latency_class"]),
        activations=_activations(row["activations"]),
        provenance=_provenance(row["provenance"]),
    )


def _source_health(row: Row) -> SourceHealth:
    return SourceHealth(
        source_key=str(row["source_key"]),
        source_url=_optional_str(row["source_url"]),
        required=bool(row["required"]),
        state=SourceState(str(row["state"])),
        attempts=int(row["attempts"]),
        last_attempt_at=_optional_datetime(row["last_attempt_at"]),
        last_success_at=_optional_datetime(row["last_success_at"]),
        last_error=_optional_str(row["last_error"]),
    )


def _provenance(value: object) -> dict[str, FactProvenance]:
    mapping = _mapping(value, "provenance")
    result: dict[str, FactProvenance] = {}
    for name, raw_provenance in mapping.items():
        provenance = _mapping(raw_provenance, f"provenance.{name}")
        result[name] = FactProvenance(
            source_key=_required_str(provenance.get("source_key"), "source_key"),
            source_url=_optional_str(provenance.get("source_url")),
            observed_at=_required_datetime(provenance.get("observed_at"), "observed_at"),
        )
    return result


def _activations(value: object) -> tuple[ActivationDescriptor, ...]:
    value = _json_value(value)
    if not isinstance(value, list):
        raise TypeError("activations must be a JSON array")
    activations: list[ActivationDescriptor] = []
    for index, raw_activation in enumerate(value):
        activation = _mapping(raw_activation, f"activations[{index}]")
        params = _mapping(activation.get("params"), f"activations[{index}].params")
        activations.append(
            ActivationDescriptor(
                kind=_required_str(activation.get("kind"), "kind"),
                surface=_required_str(activation.get("surface"), "surface"),
                params={
                    key: _required_str(param, f"params.{key}") for key, param in params.items()
                },
            )
        )
    return tuple(activations)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    value = _json_value(value)
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field} keys must be strings")
    return value


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    value = _json_value(value)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("capability list fields must be JSON string arrays")
    return tuple(value)


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError("expected an integer or null")


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _optional_bool(value: object) -> bool | None:
    return None if value is None else bool(value)


def _optional_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError("expected a string or null")


def _required_str(value: object, field: str) -> str:
    if isinstance(value, str):
        return value
    raise TypeError(f"{field} must be a string")


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _required_datetime(value, "timestamp")


def _required_datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"{field} must be a datetime or ISO timestamp")
