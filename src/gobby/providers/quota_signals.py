"""Typed quota signals and pane possible-limit hints, kept separate by confidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from gobby.providers.codex_rate_limits import (
    observation_from_rate_limits,
    observation_from_usage_limit_error,
)
from gobby.providers.quota_observations import (
    QuotaComponent,
    QuotaObservation,
    component_key,
    window_is_exhausted,
)
from gobby.providers.usage import ProviderUsageSnapshot
from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import SnapshotResult
from gobby.utils.datetime import parse_stored_datetime

# Same escape grammar as gobby.agents.prompt_detector; kept local to avoid a private import.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_LEADING_DECORATION_RE = re.compile(r"^\W+")
_RELATIVE_RESET_RE = re.compile(r"Resets in ((?:\d+[hms])+)")
_DURATION_PART_RE = re.compile(r"(\d+)([hms])")
_DURATION_UNITS = {"h": "hours", "m": "minutes", "s": "seconds"}
_AGY_ERROR_PREFIX = "Individual quota reached."
_PANE_WINDOW_LINES = 15
_OVERLAP_FLOOR = 3
_QWEN_EXHAUSTED_PREFIX = "quota exhausted: "
_QWEN_RESET_PHRASES = ("will reset", "reset at")
_PANE_FORMS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "claude": (
        ("you've hit your limit", "usage_limit"),
        ("usage limit reached", "usage_limit"),
    ),
    "codex": (("you've hit your usage limit", "usage_limit"),),
    "grok": (
        ("usage balance exhausted", "usage_limit"),
        ("usage limit reached", "usage_limit"),
        ("you hit your free usage limit", "usage_limit"),
        ("out of credits", "credits_exhausted"),
        ("run out of credits", "credits_exhausted"),
    ),
    "droid": (("standard usage limit reached", "usage_limit"),),
    "agy": (("individual quota reached", "individual_quota"),),
}


@dataclass(frozen=True, slots=True)
class PaneQuotaHint:
    """A possible-limit hint from pane text: no windows, flags, reset, or recovery."""

    provider: str
    occurrence: str
    kind: str
    detected_at: datetime


def normalize_typed_signal(
    provider: str,
    record: Mapping[str, object],
    *,
    observed_at: datetime,
    source_key: str,
) -> QuotaObservation | None:
    """Normalize one top-level raw provider envelope; None for unverified or untrusted input."""
    occurrence = _digest({"provider": provider, "record": record})
    if provider == "codex":
        return _codex_signal(
            record, observed_at=observed_at, occurrence=occurrence, source_key=source_key
        )
    if provider == "agy":
        return _agy_signal(
            record, observed_at=observed_at, occurrence=occurrence, source_key=source_key
        )
    return None


def normalize_reporter_snapshot(snapshot: ProviderUsageSnapshot) -> QuotaObservation:
    """Convert a successful usage-reporter snapshot into provider-wide numeric components."""
    components: list[QuotaComponent] = []
    for window in snapshot.windows:
        components.append(
            QuotaComponent(
                key=component_key(
                    snapshot.provider, "provider", window.label, f"window:{window.unit}"
                ),
                provider=snapshot.provider,
                limit_id=window.label,
                scope="provider",
                scope_known=True,
                state="exhausted" if window_is_exhausted(window) else "available",
                source_observed_at=snapshot.observed_at,
                occurrence=_digest(
                    {
                        "provider": snapshot.provider,
                        "observed_at": snapshot.observed_at.isoformat(),
                        "window": window.to_dict(),
                    }
                ),
                resets_at=_parse_reset(window.resets_at),
                window=window,
                verified_recovery=True,
            )
        )
    return QuotaObservation(
        provider=snapshot.provider,
        components=tuple(components),
        snapshot_at=snapshot.observed_at,
    )


def classify_pane_hint(provider: str, lines: Sequence[str]) -> str | None:
    """Return the possible-limit kind for an exact provider form in the last 15 lines."""
    block = [_clean_pane_line(line) for line in lines[-_PANE_WINDOW_LINES:]]
    if provider == "qwen":
        exhausted = any(line.startswith(_QWEN_EXHAUSTED_PREFIX) for line in block)
        guided = any(phrase in line for line in block for phrase in _QWEN_RESET_PHRASES)
        return "quota_exhausted" if exhausted and guided else None
    for line in block:
        for form, kind in _PANE_FORMS.get(provider, ()):
            if _starts_with_form(line, form):
                return kind
    return None


def pane_generation(terminal: Terminal) -> str:
    """Return the opaque ownership generation for one terminal attachment."""
    return _digest(
        {
            "terminal_id": terminal.id,
            "created_at": terminal.created_at.isoformat(),
            "attempt_generation": terminal.attempt_generation,
            "host_epoch": terminal.host_epoch,
            "session_id": terminal.session_id,
            "agent_run_id": terminal.agent_run_id,
        }
    )


class PaneOccurrenceTracker:
    """Emits hints only for complete lines appended after a verified overlap."""

    def __init__(self) -> None:
        self._prior: dict[str, tuple[str, ...]] = {}

    def observe(
        self,
        *,
        provider: str,
        generation: str,
        snapshot: SnapshotResult,
        detected_at: datetime,
    ) -> PaneQuotaHint | None:
        """Admit one capture; the first capture and any unmatched capture only rebaseline."""
        current = _complete_lines(snapshot.text)
        prior = self._prior.get(generation)
        self._prior[generation] = current
        if not prior:
            return None
        overlap = _overlap(prior, current)
        if overlap is None:
            return None
        block = current[overlap:]
        kind = classify_pane_hint(provider, block)
        if kind is None:
            return None
        occurrence = _digest(
            {
                "provider": provider,
                "generation": generation,
                "prior": _fingerprint(prior),
                "current": _fingerprint(current),
                "block": _fingerprint(block),
            }
        )
        return PaneQuotaHint(
            provider=provider, occurrence=occurrence, kind=kind, detected_at=detected_at
        )

    def rebaseline(self, generation: str) -> None:
        """Forget one generation after a failed or replaced capture."""
        self._prior.pop(generation, None)


def _codex_signal(
    record: Mapping[str, object],
    *,
    observed_at: datetime,
    occurrence: str,
    source_key: str,
) -> QuotaObservation | None:
    payload = record.get("payload")
    if record.get("type") != "event_msg" or not isinstance(payload, Mapping):
        return None
    if payload.get("type") == "token_count":
        return observation_from_rate_limits(
            payload.get("rate_limits"),
            source_observed_at=observed_at,
            occurrence=occurrence,
            source_key=source_key,
        )
    if payload.get("type") == "task_complete":
        return observation_from_usage_limit_error(
            payload.get("error"),
            source_observed_at=observed_at,
            occurrence=occurrence,
            source_key=source_key,
        )
    return None


def _agy_signal(
    record: Mapping[str, object],
    *,
    observed_at: datetime,
    occurrence: str,
    source_key: str,
) -> QuotaObservation | None:
    text = record.get("error")
    if record.get("status") != "ERROR" or not isinstance(text, str):
        return None
    if not text.startswith(_AGY_ERROR_PREFIX):
        return None
    duration = _relative_reset(text)
    if duration is None:
        return None
    scope = f"source:{source_key}"
    component = QuotaComponent(
        key=component_key("agy", scope, "agy", "hard:individual_quota"),
        provider="agy",
        limit_id="agy",
        scope=scope,
        scope_known=False,
        state="exhausted",
        source_observed_at=observed_at,
        occurrence=occurrence,
        resets_at=observed_at + duration,
    )
    return QuotaObservation(provider="agy", components=(component,))


def _relative_reset(text: str) -> timedelta | None:
    match = _RELATIVE_RESET_RE.search(text)
    if match is None:
        return None
    total = timedelta()
    for amount, unit in _DURATION_PART_RE.findall(match.group(1)):
        total += timedelta(**{_DURATION_UNITS[unit]: int(amount)})
    return total if total > timedelta() else None


def _parse_reset(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_stored_datetime(value)
    except ValueError:
        return None


def _clean_pane_line(line: str) -> str:
    stripped = _LEADING_DECORATION_RE.sub("", _ANSI_ESCAPE_RE.sub("", line))
    return stripped.rstrip().casefold()


def _starts_with_form(line: str, form: str) -> bool:
    if not line.startswith(form):
        return False
    return len(line) == len(form) or not line[len(form)].isalnum()


def _complete_lines(text: str) -> tuple[str, ...]:
    """Only newline-terminated lines count; a trailing partial line is not yet complete."""
    return tuple(_ANSI_ESCAPE_RE.sub("", line).rstrip() for line in text.split("\n")[:-1])


def _overlap(prior: Sequence[str], current: Sequence[str]) -> int | None:
    floor = min(_OVERLAP_FLOOR, len(prior))
    for size in range(min(len(prior), len(current)), floor - 1, -1):
        if prior[-size:] == current[:size]:
            return size
    return None


def _fingerprint(lines: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
