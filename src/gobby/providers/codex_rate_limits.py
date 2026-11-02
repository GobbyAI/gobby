"""Codex rate-limit and usage-limit observations from native transcript envelopes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime

from gobby.providers.quota_observations import (
    QuotaComponent,
    QuotaObservation,
    component_key,
)
from gobby.providers.usage import UsageWindow

_PROVIDER = "codex"
_DEFAULT_LIMIT_ID = "codex"
_WEEKLY_MINUTES = 10080
_FIVE_HOUR_MINUTES = 300
_PERCENT_LIMIT = 100.0
# RateLimitReachedType values from the Codex core protocol; any other value is unverified.
_VERIFIED_REACHED_TYPES = frozenset(
    {
        "rate_limit_reached",
        "workspace_owner_credits_depleted",
        "workspace_member_credits_depleted",
        "workspace_owner_usage_limit_reached",
        "workspace_member_usage_limit_reached",
    }
)


def observation_from_rate_limits(
    rate_limits: object,
    *,
    source_observed_at: datetime,
    occurrence: str,
    source_key: str,
) -> QuotaObservation | None:
    """Normalize one Codex token_count rate_limits payload; None when nothing verified."""
    if not isinstance(rate_limits, Mapping):
        return None
    limit_id = _limit_id(rate_limits.get("limit_id"))
    if limit_id is None:
        return None
    scope = f"source:{source_key}"
    metadata = _metadata(rate_limits)
    components: list[QuotaComponent] = []
    for name in ("primary", "secondary"):
        raw_window = rate_limits.get(name)
        if raw_window is None:
            continue
        component = _window_component(
            raw_window,
            limit_id=limit_id,
            scope=scope,
            source_observed_at=source_observed_at,
            occurrence=occurrence,
            metadata=metadata,
        )
        if component is None:
            return None
        components.append(component)
    reached = rate_limits.get("rate_limit_reached_type")
    if reached is not None:
        if not isinstance(reached, str) or reached not in _VERIFIED_REACHED_TYPES:
            return None
        components.append(
            _hard_component(
                "hard:rate_limit_reached",
                limit_id=limit_id,
                scope=scope,
                source_observed_at=source_observed_at,
                occurrence=occurrence,
                metadata=metadata,
            )
        )
    if not components or len({component.key for component in components}) != len(components):
        return None
    return QuotaObservation(provider=_PROVIDER, components=tuple(components))


def observation_from_usage_limit_error(
    error_info: object,
    *,
    source_observed_at: datetime,
    occurrence: str,
    source_key: str,
) -> QuotaObservation | None:
    """Normalize a task_complete error whose codex_error_info is usage_limit_exceeded."""
    if not isinstance(error_info, Mapping):
        return None
    if error_info.get("codex_error_info") != "usage_limit_exceeded":
        return None
    component = _hard_component(
        "hard:usage_limit_exceeded",
        limit_id=_DEFAULT_LIMIT_ID,
        scope=f"source:{source_key}",
        source_observed_at=source_observed_at,
        occurrence=occurrence,
        metadata={},
    )
    return QuotaObservation(provider=_PROVIDER, components=(component,))


def _limit_id(value: object) -> str | None:
    if value is None:
        return _DEFAULT_LIMIT_ID
    if isinstance(value, str) and value:
        return value
    return None


def _window_component(
    raw: object,
    *,
    limit_id: str,
    scope: str,
    source_observed_at: datetime,
    occurrence: str,
    metadata: Mapping[str, object],
) -> QuotaComponent | None:
    if not isinstance(raw, Mapping):
        return None
    used = _number(raw.get("used_percent"))
    minutes = raw.get("window_minutes")
    if used is None or used < 0:
        return None
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        return None
    window = UsageWindow(
        label=_window_label(minutes),
        used=used,
        limit=_PERCENT_LIMIT,
        unit="percent",
        resets_at=None,
    )
    return QuotaComponent(
        key=component_key(_PROVIDER, scope, limit_id, f"w:{minutes}"),
        provider=_PROVIDER,
        limit_id=limit_id,
        scope=scope,
        scope_known=False,
        state="exhausted" if used >= _PERCENT_LIMIT else "available",
        source_observed_at=source_observed_at,
        occurrence=occurrence,
        resets_at=_reset_time(raw.get("resets_at")),
        window=window,
        metadata=metadata,
    )


def _hard_component(
    identity: str,
    *,
    limit_id: str,
    scope: str,
    source_observed_at: datetime,
    occurrence: str,
    metadata: Mapping[str, object],
) -> QuotaComponent:
    return QuotaComponent(
        key=component_key(_PROVIDER, scope, limit_id, identity),
        provider=_PROVIDER,
        limit_id=limit_id,
        scope=scope,
        scope_known=False,
        state="exhausted",
        source_observed_at=source_observed_at,
        occurrence=occurrence,
        metadata=metadata,
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _reset_time(value: object) -> datetime | None:
    seconds = _number(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _window_label(minutes: int) -> str:
    if minutes == _WEEKLY_MINUTES:
        return "weekly"
    if minutes == _FIVE_HOUR_MINUTES:
        return "five_hour"
    return f"{minutes}m"


def _metadata(rate_limits: Mapping[str, object]) -> dict[str, object]:
    """Keep the allowlisted plan and credit facts; drop every other payload field."""
    metadata: dict[str, object] = {}
    plan_type = rate_limits.get("plan_type")
    if isinstance(plan_type, str) and plan_type:
        metadata["plan_type"] = plan_type
    credits = rate_limits.get("credits")
    if isinstance(credits, Mapping):
        allowed: dict[str, object] = {}
        for name in ("has_credits", "unlimited"):
            flag = credits.get(name)
            if isinstance(flag, bool):
                allowed[name] = flag
        balance = credits.get("balance")
        if isinstance(balance, str):
            allowed["balance"] = balance
        if allowed:
            metadata["credits"] = allowed
    return metadata
