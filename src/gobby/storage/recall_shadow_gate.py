"""Atomic holdout reservation for the recall ship gate."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from uuid import uuid4

from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

GATE_RESERVATION_LEASE = timedelta(minutes=10)


@dataclass(frozen=True)
class GateHoldoutReservation:
    """Globally serialize request-ID consumption across all gate cohorts."""

    PRIORITY: ClassVar[int] = 930


@dataclass(frozen=True)
class GateReservationResult:
    """Outcome of an atomic holdout reservation attempt."""

    status: Literal["reserved", "in_progress", "complete", "insufficient"]
    request_ids: tuple[str, ...] = ()
    claim_token: str | None = None
    ship: bool | None = None
    decision: dict[str, Any] | None = None


def _json_object(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("stored gate decision must be a JSON object")


class RecallShadowGateStoreMixin:
    """Storage API for once-only gate holdout consumption."""

    db: HubDatabase

    def reserve_gate_holdout(
        self,
        *,
        holdout_consumption_key: str,
        fit_settings_digest: str,
        holdout_partition_ids: Sequence[str],
        min_requests: int,
        now: datetime | None = None,
    ) -> GateReservationResult:
        """Reserve fresh holdout request IDs before any holdout rows are read."""
        if not holdout_consumption_key or not fit_settings_digest:
            raise ValueError("holdout_consumption_key and fit_settings_digest are required")
        if min_requests <= 0:
            raise ValueError("min_requests must be positive")
        current_time = now or utc_now()
        with self.db.transaction_immediate(GateHoldoutReservation()) as transaction:
            existing = transaction.execute(
                """
                SELECT status, fit_settings_digest, claim_token, lease_expires_at,
                       ship, decision
                FROM recall_gate_runs
                WHERE holdout_consumption_key = %s
                """,
                (holdout_consumption_key,),
            ).fetchone()
            if existing is not None:
                if existing["fit_settings_digest"] != fit_settings_digest:
                    raise ValueError(
                        "fit_settings_digest conflicts with the persisted gate reservation"
                    )
                reserved_ids = self._reserved_request_ids(transaction, holdout_consumption_key)
                if existing["status"] == "complete":
                    return GateReservationResult(
                        status="complete",
                        request_ids=reserved_ids,
                        ship=bool(existing["ship"]),
                        decision=_json_object(existing["decision"]),
                    )
                lease_expires_at = existing["lease_expires_at"]
                if lease_expires_at is not None and lease_expires_at > current_time:
                    return GateReservationResult(status="in_progress")
                claim_token = str(uuid4())
                reclaimed = transaction.execute(
                    """
                    UPDATE recall_gate_runs
                    SET claim_token = %s, lease_expires_at = %s,
                        attempts = attempts + 1, last_error = NULL, updated_at = %s
                    WHERE holdout_consumption_key = %s
                      AND status = 'reserved'
                      AND fit_settings_digest = %s
                    """,
                    (
                        claim_token,
                        current_time + GATE_RESERVATION_LEASE,
                        current_time,
                        holdout_consumption_key,
                        fit_settings_digest,
                    ),
                )
                if reclaimed.rowcount != 1:
                    raise RuntimeError("gate reservation changed during lease reclaim")
                return GateReservationResult(
                    status="reserved",
                    request_ids=reserved_ids,
                    claim_token=claim_token,
                )

            candidate_ids = tuple(dict.fromkeys(str(value) for value in holdout_partition_ids))
            consumed = {
                str(row["request_id"])
                for row in transaction.execute(
                    "SELECT request_id FROM recall_holdout_consumed"
                ).fetchall()
            }
            fresh_ids = tuple(
                request_id for request_id in candidate_ids if request_id not in consumed
            )
            if len(fresh_ids) < min_requests:
                return GateReservationResult(status="insufficient")

            claim_token = str(uuid4())
            transaction.execute(
                """
                INSERT INTO recall_gate_runs
                    (holdout_consumption_key, status, fit_settings_digest,
                     claim_token, lease_expires_at, attempts)
                VALUES (%s, 'reserved', %s, %s, %s, 1)
                """,
                (
                    holdout_consumption_key,
                    fit_settings_digest,
                    claim_token,
                    current_time + GATE_RESERVATION_LEASE,
                ),
            )
            transaction.executemany(
                """
                INSERT INTO recall_holdout_consumed
                    (request_id, holdout_consumption_key, consumed_at)
                VALUES (%s, %s, %s)
                """,
                [(request_id, holdout_consumption_key, current_time) for request_id in fresh_ids],
            )
            return GateReservationResult(
                status="reserved",
                request_ids=fresh_ids,
                claim_token=claim_token,
            )

    def complete_gate_run(
        self,
        holdout_consumption_key: str,
        claim_token: str,
        *,
        ship: bool,
        decision: Mapping[str, Any],
        now: datetime | None = None,
    ) -> GateReservationResult:
        """Complete a live gate reservation exactly once."""
        current_time = now or utc_now()
        with self.db.transaction_immediate(GateHoldoutReservation()) as transaction:
            existing = transaction.execute(
                """
                SELECT status, claim_token, ship, decision
                FROM recall_gate_runs WHERE holdout_consumption_key = %s
                """,
                (holdout_consumption_key,),
            ).fetchone()
            if existing is None:
                raise ValueError("gate reservation does not exist")
            request_ids = self._reserved_request_ids(transaction, holdout_consumption_key)
            if existing["status"] == "complete":
                return GateReservationResult(
                    status="complete",
                    request_ids=request_ids,
                    ship=bool(existing["ship"]),
                    decision=_json_object(existing["decision"]),
                )
            if existing["claim_token"] != claim_token:
                raise ValueError("stale gate claim_token")
            completed = transaction.execute(
                """
                UPDATE recall_gate_runs
                SET status = 'complete', ship = %s, decision = %s,
                    lease_expires_at = NULL, last_error = NULL, updated_at = %s
                WHERE holdout_consumption_key = %s
                  AND status = 'reserved' AND claim_token = %s
                """,
                (
                    ship,
                    json.dumps(dict(decision), sort_keys=True, separators=(",", ":")),
                    current_time,
                    holdout_consumption_key,
                    claim_token,
                ),
            )
            if completed.rowcount != 1:
                raise RuntimeError("gate reservation changed while completing")
            return GateReservationResult(
                status="complete",
                request_ids=request_ids,
                ship=ship,
                decision=dict(decision),
            )

    @staticmethod
    def _reserved_request_ids(transaction: Any, key: str) -> tuple[str, ...]:
        rows = transaction.execute(
            """
            SELECT request_id FROM recall_holdout_consumed
            WHERE holdout_consumption_key = %s ORDER BY id
            """,
            (key,),
        ).fetchall()
        return tuple(str(row["request_id"]) for row in rows)
