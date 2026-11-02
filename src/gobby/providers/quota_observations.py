"""Provider-neutral quota components: admission, persistence shape, and freshness reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from gobby.providers.usage import UsageWindow
from gobby.utils.datetime import parse_stored_datetime

type ComponentState = Literal["available", "exhausted"]
type AdmissionOutcome = Literal[
    "accepted",
    "duplicate",
    "ambiguous",
    "older",
    "earlier_reset",
    "unverified_recovery",
]
type QuotaState = Literal["available", "exhausted", "stale", "unknown"]

FRESHNESS_SECONDS = 900.0


@dataclass(frozen=True, slots=True)
class QuotaComponent:
    """One independently timed limit reading keyed by provider, scope, and limit identity."""

    key: str
    provider: str
    limit_id: str
    scope: str
    scope_known: bool
    state: ComponentState
    source_observed_at: datetime
    occurrence: str
    resets_at: datetime | None = None
    window: UsageWindow | None = None
    verified_recovery: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    """Verified components from one raw provider envelope or reporter snapshot."""

    provider: str
    components: tuple[QuotaComponent, ...]
    source_version: str = "unknown"


@dataclass(frozen=True, slots=True)
class QuotaAggregate:
    """Read-time view over retained components; windows contain only fresh numeric components."""

    state: QuotaState
    windows: tuple[UsageWindow, ...]
    reason: str | None
    observed_at: datetime | None
    stale_keys: tuple[str, ...]
    details: Mapping[str, object] = field(default_factory=dict)


def component_key(provider: str, scope: str, limit_id: str, identity: str) -> str:
    """Return the storage key for one limit; identity is a window type or hard-signal family."""
    return "|".join((provider, scope, limit_id, identity))


def window_is_exhausted(window: UsageWindow) -> bool:
    """A numeric window is exhausted once used reaches a positive limit."""
    return window.limit > 0 and window.used >= window.limit


def admit_component(existing: QuotaComponent | None, incoming: QuotaComponent) -> AdmissionOutcome:
    """Decide whether one incoming reading may replace the retained component."""
    if existing is None:
        return "accepted"
    if incoming.occurrence == existing.occurrence:
        return "duplicate"
    if incoming.source_observed_at < existing.source_observed_at:
        return "older"
    if (
        incoming.resets_at is not None
        and existing.resets_at is not None
        and incoming.resets_at < existing.resets_at
    ):
        return "earlier_reset"
    if incoming.source_observed_at == existing.source_observed_at:
        return "duplicate" if _same_reading(existing, incoming) else "ambiguous"
    if (
        existing.state == "exhausted"
        and incoming.state != "exhausted"
        and not _recovery_verified(existing, incoming)
    ):
        return "unverified_recovery"
    return "accepted"


def merge_components(
    existing: Mapping[str, QuotaComponent],
    incoming: Sequence[QuotaComponent],
) -> tuple[dict[str, QuotaComponent], dict[str, AdmissionOutcome]]:
    """Admit each incoming component independently and return the merged map with outcomes."""
    merged = dict(existing)
    outcomes: dict[str, AdmissionOutcome] = {}
    for component in incoming:
        outcome = admit_component(merged.get(component.key), component)
        outcomes[component.key] = outcome
        if outcome == "accepted":
            merged[component.key] = component
    return merged, outcomes


def encode_quota_details(
    components: Mapping[str, QuotaComponent],
    pane_hints: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    """Serialize components and pane-hint dedupe records into the row details column."""
    return {
        "quota_components": {key: _encode_component(c) for key, c in components.items()},
        "pane_hints": {occurrence: dict(hint) for occurrence, hint in pane_hints.items()},
    }


def decode_quota_details(
    details: Mapping[str, object],
) -> tuple[dict[str, QuotaComponent], dict[str, dict[str, str]]]:
    """Parse the row details column back into components and pane-hint dedupe records."""
    raw_components = details.get("quota_components", {})
    raw_hints = details.get("pane_hints", {})
    if not isinstance(raw_components, Mapping) or not isinstance(raw_hints, Mapping):
        raise TypeError("provider capacity details must hold mapping sections")
    components = {
        str(key): _decode_component(str(key), value) for key, value in raw_components.items()
    }
    pane_hints = {str(key): _decode_hint(value) for key, value in raw_hints.items()}
    return components, pane_hints


def aggregate_components(
    components: Mapping[str, QuotaComponent],
    *,
    now: datetime,
) -> QuotaAggregate:
    """Age every component independently and derive the provider-level read."""
    if not components:
        return QuotaAggregate(
            state="unknown", windows=(), reason=None, observed_at=None, stale_keys=()
        )
    ordered = sorted(components)
    reasons = {key: _stale_reason(components[key], now) for key in ordered}
    fresh = [components[key] for key in ordered if reasons[key] is None]
    stale_keys = tuple(key for key in ordered if reasons[key] is not None)
    details = {
        "components": {key: _component_detail(components[key], reasons[key]) for key in ordered}
    }
    if any(component.state == "exhausted" for component in fresh):
        state: QuotaState = "exhausted"
        reason: str | None = "one or more usage windows exhausted"
    elif stale_keys:
        state = "stale"
        reason = "quota observation is stale"
    else:
        state = "available"
        reason = None
    return QuotaAggregate(
        state=state,
        windows=tuple(c.window for c in fresh if c.window is not None),
        reason=reason,
        observed_at=max(c.source_observed_at for c in components.values()),
        stale_keys=stale_keys,
        details=details,
    )


def _same_reading(existing: QuotaComponent, incoming: QuotaComponent) -> bool:
    return (
        existing.state == incoming.state
        and existing.window == incoming.window
        and existing.resets_at == incoming.resets_at
    )


def _recovery_verified(existing: QuotaComponent, incoming: QuotaComponent) -> bool:
    if incoming.verified_recovery:
        return True
    return (
        incoming.resets_at is not None
        and existing.resets_at is not None
        and incoming.resets_at > existing.resets_at
    )


def _stale_reason(component: QuotaComponent, now: datetime) -> str | None:
    if (now - component.source_observed_at).total_seconds() > FRESHNESS_SECONDS:
        return "source_older_than_900s"
    if component.resets_at is not None and component.resets_at <= now:
        return "reset_passed"
    return None


def _component_detail(component: QuotaComponent, stale_reason: str | None) -> dict[str, object]:
    return {
        "limit_id": component.limit_id,
        "scope": component.scope,
        "state": component.state,
        "fresh": stale_reason is None,
        "stale_reason": stale_reason,
        "source_observed_at": component.source_observed_at.isoformat(),
        "resets_at": None if component.resets_at is None else component.resets_at.isoformat(),
    }


def _encode_component(component: QuotaComponent) -> dict[str, object]:
    return {
        "provider": component.provider,
        "limit_id": component.limit_id,
        "scope": component.scope,
        "scope_known": component.scope_known,
        "state": component.state,
        "source_observed_at": component.source_observed_at.isoformat(),
        "occurrence": component.occurrence,
        "resets_at": None if component.resets_at is None else component.resets_at.isoformat(),
        "window": None if component.window is None else component.window.to_dict(),
        "verified_recovery": component.verified_recovery,
        "metadata": dict(component.metadata),
    }


def _decode_component(key: str, value: object) -> QuotaComponent:
    if not isinstance(value, Mapping):
        raise TypeError(f"persisted quota component {key!r} must be a mapping")
    source_observed_at = parse_stored_datetime(value.get("source_observed_at"))
    if source_observed_at is None:
        raise TypeError(f"persisted quota component {key!r} needs source_observed_at")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise TypeError(f"persisted quota component {key!r} metadata must be a mapping")
    return QuotaComponent(
        key=key,
        provider=_text(value, "provider", key),
        limit_id=_text(value, "limit_id", key),
        scope=_text(value, "scope", key),
        scope_known=_flag(value, "scope_known", key),
        state=_component_state(value.get("state"), key),
        source_observed_at=source_observed_at,
        occurrence=_text(value, "occurrence", key),
        resets_at=parse_stored_datetime(value.get("resets_at")),
        window=_decode_window(value.get("window"), key),
        verified_recovery=_flag(value, "verified_recovery", key),
        metadata=dict(metadata),
    )


def _decode_window(value: object, key: str) -> UsageWindow | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"persisted quota component {key!r} window must be a mapping")
    used = value.get("used")
    limit = value.get("limit")
    resets_at = value.get("resets_at")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        raise TypeError(f"persisted quota component {key!r} window used must be numeric")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool):
        raise TypeError(f"persisted quota component {key!r} window limit must be numeric")
    if resets_at is not None and not isinstance(resets_at, str):
        raise TypeError(f"persisted quota component {key!r} window resets_at must be a string")
    return UsageWindow(
        label=_text(value, "label", key),
        used=float(used),
        limit=float(limit),
        unit=_text(value, "unit", key),
        resets_at=resets_at,
    )


def _decode_hint(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(name, str) and isinstance(item, str) for name, item in value.items()
    ):
        raise TypeError("persisted pane hint must be a mapping of strings")
    return {str(name): str(item) for name, item in value.items()}


def _component_state(value: object, key: str) -> ComponentState:
    if value == "available":
        return "available"
    if value == "exhausted":
        return "exhausted"
    raise ValueError(f"persisted quota component {key!r} has invalid state {value!r}")


def _text(value: Mapping[str, object], name: str, key: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise TypeError(f"persisted quota component {key!r} {name} must be a non-empty string")
    return item


def _flag(value: Mapping[str, object], name: str, key: str) -> bool:
    item = value.get(name)
    if not isinstance(item, bool):
        raise TypeError(f"persisted quota component {key!r} {name} must be a boolean")
    return item
