"""Caught-up live quota collection from raw transcript envelopes (plan 4.3).

A pass observes quota only when the session's previous cursor-advancing pass ended at
the transcript end and this pass reaches it again. Backlog, rebuilds, replays and
history reads therefore never produce a new observation, and only envelopes inside the
freshness window count.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.providers.quota_observations import (
    FRESHNESS_SECONDS,
    QuotaComponent,
    QuotaObservation,
)
from gobby.providers.quota_signals import normalize_typed_signal
from gobby.sessions.turn_lifecycle import TurnLifecycleReducer

if TYPE_CHECKING:
    from gobby.sessions.processor_types import ProcessorHost
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def newest_live_observation(
    provider: str,
    raw_lines: Sequence[str],
    *,
    now: datetime,
    source_key: str,
) -> QuotaObservation | None:
    """Fold the fresh quota envelopes in ``raw_lines`` into one observation, newest wins."""
    newest: dict[str, QuotaComponent] = {}
    for raw_line in raw_lines:
        record = _load_record(raw_line)
        if record is None:
            continue
        observed_at = _envelope_time(record)
        if observed_at is None or abs(now - observed_at).total_seconds() > FRESHNESS_SECONDS:
            continue
        observation = normalize_typed_signal(
            provider, record, observed_at=observed_at, source_key=source_key
        )
        if observation is None:
            continue
        for component in observation.components:
            current = newest.get(component.key)
            if current is None or component.source_observed_at >= current.source_observed_at:
                newest[component.key] = component
    if not newest:
        return None
    return QuotaObservation(provider=provider, components=tuple(newest.values()))


async def collect_live_quota(
    host: ProcessorHost,
    session_id: str,
    provider: str,
    raw_lines: Sequence[str],
) -> None:
    """Observe the fresh quota in one caught-up pass; failures are logged, never raised."""
    service = host.provider_capacity_service
    session_manager = host.session_manager
    if service is None or session_manager is None:
        return
    try:
        now = datetime.now(UTC)
        # Most passes carry no quota envelope, so the turn-generation read, a database
        # round trip, runs only when the probe finds an observation to admit.
        if newest_live_observation(provider, raw_lines, now=now, source_key=session_id) is None:
            return
        generation = await host._run_db(_turn_generation, session_manager, session_id)
        observation = newest_live_observation(
            provider, raw_lines, now=now, source_key=f"{session_id}:{generation}"
        )
        if observation is not None:
            await service.observe(observation)
    except Exception:
        logger.warning("Live quota collection failed for %s", session_id, exc_info=True)


def latch_quota_end(caught_up_sessions: set[str], session_id: str, caught_up: bool) -> None:
    """Record whether the cursor-advancing pass that just ended reached the transcript end."""
    if caught_up:
        caught_up_sessions.add(session_id)
    else:
        caught_up_sessions.discard(session_id)


def _turn_generation(session_manager: SessionManager, session_id: str) -> int:
    return TurnLifecycleReducer(session_manager).get(session_id).generation


def _load_record(raw_line: str) -> Mapping[str, Any] | None:
    try:
        record = json.loads(raw_line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _envelope_time(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        observed_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return observed_at if observed_at.tzinfo is not None else None
