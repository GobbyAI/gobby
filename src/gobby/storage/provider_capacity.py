"""Machine-scoped persistence for latest successful provider capacity."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

type PersistedCapacityState = Literal["available", "exhausted"]
type Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ProviderCapacityRecord:
    """Latest persisted successful observation plus its age at read time."""

    machine_id: str
    provider: str
    state: PersistedCapacityState
    observed_at: datetime
    windows: tuple[dict[str, object], ...]
    reason: str | None
    source_version: str
    age_seconds: float


class ProviderCapacityStorage:
    """DML owner for ``provider_capacity_snapshots``."""

    def __init__(self, db: HubDatabase, *, machine_id: str, clock: Clock = utc_now) -> None:
        self._db = db
        self.machine_id = str(UUID(machine_id))
        self._clock = clock

    def upsert(
        self,
        *,
        provider: str,
        state: PersistedCapacityState,
        observed_at: datetime,
        windows: Sequence[Mapping[str, object]],
        reason: str | None,
        source_version: str,
    ) -> None:
        """Replace one machine/provider row with its latest successful observation."""
        self._db.execute(
            """
            INSERT INTO provider_capacity_snapshots (
                machine_id,
                provider,
                state,
                observed_at,
                windows,
                reason,
                source_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (machine_id, provider) DO UPDATE SET
                state = excluded.state,
                observed_at = excluded.observed_at,
                windows = excluded.windows,
                reason = excluded.reason,
                source_version = excluded.source_version
            """,
            (
                self.machine_id,
                provider,
                state,
                observed_at,
                Jsonb([dict(window) for window in windows]),
                reason,
                source_version,
            ),
        )

    def get(self, provider: str) -> ProviderCapacityRecord | None:
        """Read one latest observation with its age at the current storage clock."""
        row = self._db.fetchone(
            """
            SELECT machine_id, provider, state, observed_at, windows, reason, source_version
            FROM provider_capacity_snapshots
            WHERE machine_id = %s AND provider = %s
            """,
            (self.machine_id, provider),
        )
        if row is None:
            return None
        state = row["state"]
        if state not in ("available", "exhausted"):
            raise ValueError(f"invalid persisted provider capacity state: {state!r}")
        observed_at = row["observed_at"]
        if not isinstance(observed_at, datetime):
            raise TypeError("provider capacity observed_at must be a datetime")
        source_version = row["source_version"]
        if not isinstance(source_version, str) or not source_version:
            raise TypeError("provider capacity source_version must be a non-empty string")
        return ProviderCapacityRecord(
            machine_id=str(row["machine_id"]),
            provider=str(row["provider"]),
            state=state,
            observed_at=observed_at,
            windows=_decode_windows(row["windows"]),
            reason=None if row["reason"] is None else str(row["reason"]),
            source_version=source_version,
            age_seconds=max(0.0, (self._clock() - observed_at).total_seconds()),
        )


def _decode_windows(value: object) -> tuple[dict[str, object], ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("provider capacity windows contain invalid JSON") from error
    if not isinstance(value, list):
        raise TypeError("provider capacity windows must be a JSON array")
    windows: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise TypeError(f"provider capacity window {index} must be a JSON object")
        windows.append(dict(item))
    return tuple(windows)
