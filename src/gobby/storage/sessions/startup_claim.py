"""Durable startup-context claim generation on the sessions row.

Every transition is one compare-and-swap ``UPDATE`` keyed on the current
``startup_claim_state`` (plus generation and owner for commit, rollback, and
invalidate), so concurrent SessionStart workers cannot both win the same
generation. ``context_injected`` is derived: only a commit sets it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

StartupClaimState = Literal["idle", "claimed", "committed", "invalidated"]

_CLAIM_COLUMNS = "startup_claim_generation, startup_claim_owner, startup_claim_state"
_CLAIMABLE_STATES = ("idle", "invalidated")
_CLAIM_ATTEMPTS = 3


@dataclass(frozen=True)
class StartupContextClaim:
    """Token-bearing startup-context claim generation."""

    mode: Literal["full", "live"]
    generation: int
    owner_token: str | None
    state: StartupClaimState


def _claim_from_row(row: Any, *, token: str) -> StartupContextClaim:
    generation = int(row["startup_claim_generation"] or 0)
    owner = row["startup_claim_owner"]
    owner_token = str(owner) if owner else None
    state: StartupClaimState = row["startup_claim_state"] or "idle"
    if state == "claimed" and owner_token == token:
        return StartupContextClaim("full", generation, token, "claimed")
    if state == "claimed":
        return StartupContextClaim("live", generation, owner_token, "claimed")
    if state == "committed":
        return StartupContextClaim("live", generation, owner_token, "committed")
    return StartupContextClaim("full", generation, owner_token, state)


def claim_startup_context(
    db: HubDatabase,
    session_id: str,
    *,
    owner_token: str,
) -> StartupContextClaim:
    """Allocate the next generation, adopt a live claim by owner, or observe live.

    Idle and invalidated rows take a new generation for ``owner_token`` and
    report ``full``. A row already claimed by the same owner adopts (``full``);
    any other live or committed claim reports ``live`` without mutation. A
    session that does not exist reports ``full`` at generation 0 with no owner
    so callers never treat it as a durable claim.
    """

    with db.transaction() as conn:
        for _ in range(_CLAIM_ATTEMPTS):
            row = conn.execute(
                "UPDATE sessions SET startup_claim_generation = startup_claim_generation + 1, "
                "startup_claim_owner = %s, startup_claim_state = 'claimed', updated_at = %s "
                "WHERE id = %s AND startup_claim_state = ANY(%s) "
                f"RETURNING {_CLAIM_COLUMNS}",
                (owner_token, utc_now(), session_id, list(_CLAIMABLE_STATES)),
            ).fetchone()
            if row is not None:
                return _claim_from_row(row, token=owner_token)
            observed = conn.execute(
                f"SELECT {_CLAIM_COLUMNS} FROM sessions WHERE id = %s",
                (session_id,),
            ).fetchone()
            if observed is None:
                return StartupContextClaim("full", 0, None, "idle")
            if observed["startup_claim_state"] not in _CLAIMABLE_STATES:
                return _claim_from_row(observed, token=owner_token)
        return _claim_from_row(observed, token=owner_token)


def _transition(
    db: HubDatabase,
    session_id: str,
    generation: int,
    owner_token: str,
    *,
    assignments: str,
) -> bool:
    with db.transaction() as conn:
        row = conn.execute(
            f"UPDATE sessions SET {assignments}, updated_at = %s "
            "WHERE id = %s AND startup_claim_state = 'claimed' "
            "AND startup_claim_generation = %s AND startup_claim_owner = %s "
            "RETURNING id",
            (utc_now(), session_id, generation, owner_token),
        ).fetchone()
    return row is not None


def commit_startup_context(
    db: HubDatabase,
    session_id: str,
    generation: int,
    owner_token: str,
) -> bool:
    """CAS a matching claimed generation to committed and derive context_injected."""

    return _transition(
        db,
        session_id,
        generation,
        owner_token,
        assignments="startup_claim_state = 'committed', context_injected = TRUE",
    )


def rollback_startup_context(
    db: HubDatabase,
    session_id: str,
    generation: int,
    owner_token: str,
) -> bool:
    """CAS a matching claimed generation back to idle so a later turn re-claims."""

    return _transition(
        db,
        session_id,
        generation,
        owner_token,
        assignments="startup_claim_state = 'idle', startup_claim_owner = NULL",
    )


def invalidate_startup_context(
    db: HubDatabase,
    session_id: str,
    generation: int,
    owner_token: str,
) -> bool:
    """CAS a matching claimed generation to invalidated (owner retained for audit)."""

    return _transition(
        db,
        session_id,
        generation,
        owner_token,
        assignments="startup_claim_state = 'invalidated'",
    )
