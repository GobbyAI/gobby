"""The fleet health state file that the watcher writes and admission reads.

Contract: ``GOBBY_HOME/state/fleet-health.json``, schema version 1. The watcher rewrites it
atomically on every sample. A missing, unreadable, malformed, or stale file is unstable:
admission must never treat an unknown fleet as healthy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gobby.paths import get_gobby_home

STALE_AFTER_SECONDS = 90
UNSTABLE = "unstable"


def fleet_state_path() -> Path:
    """The state file the watcher writes under the Gobby home."""
    return get_gobby_home() / "state" / "fleet-health.json"


@dataclass(frozen=True)
class FleetState:
    """The fleet verdict admission acts on."""

    state: str
    reasons: tuple[str, ...]
    last_non_healthy_at_unix: float | None = None


class _StateDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1]
    state: Literal["healthy", "degraded", "unstable"]
    sampled_at_unix: float
    last_non_healthy_at_unix: float | None = None
    reasons: list[str] = Field(default_factory=list)


def read_fleet_state(path: Path, now: float) -> FleetState:
    """Read the state file. Anything other than a fresh, well-formed file is unstable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _unstable("state_file_missing")
    except OSError as exc:
        return _unstable(f"state_file_unreadable: {exc.strerror or type(exc).__name__}")
    try:
        document = _StateDocument.model_validate_json(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        return _unstable(f"state_file_unreadable: invalid field(s) {', '.join(fields)}")

    age = now - document.sampled_at_unix
    if abs(age) > STALE_AFTER_SECONDS:
        return _unstable(f"state_file_stale: sampled {age:.0f}s from now")
    return FleetState(
        state=document.state,
        reasons=tuple(document.reasons),
        last_non_healthy_at_unix=document.last_non_healthy_at_unix,
    )


def _unstable(reason: str) -> FleetState:
    return FleetState(state=UNSTABLE, reasons=(reason,))
