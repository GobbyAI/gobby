"""The in-doubt owner of a placed spawn: settle a terminal only on proof.

One owner task per claimed attempt drains the stage task it was handed, proves
the kill or the absence, writes one settlement, confirms it by read-back and
only then releases the claim. A settlement or proof it cannot confirm is
retried on a capped backoff for as long as the daemon lives. Cancelling the
owner task itself is daemon shutdown: it releases nothing and runs no
deferred step, so restart recovery owns the row.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from gobby.storage.terminal_settlement import OrphanIdentity
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.in_doubt import DeferredStep, in_doubt_spawns
from gobby.terminals.runtime import PreparedSpawn, TerminalRuntime

logger = logging.getLogger(__name__)

OwnerStage = Literal["create", "bind", "reserve", "prepare", "promote"]
Settlement = Literal["exited", "orphan"]
AttemptPair = tuple[int, datetime]

IMMEDIATE_RETRIES = 3
BACKOFF_START_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
WARNING_INTERVAL_SECONDS = 600.0
KILL_GRACE_SECONDS = 1.0

# The backoff wait; tests replace it to run the retry without real time.
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class InDoubtAttempt:
    """What the owner holds in its own frame for one claimed attempt."""

    manager: TerminalManager
    runtime: TerminalRuntime
    backend: str
    terminal_id: str
    spawn_key: str
    pair: AttemptPair | None = None
    # A retry's pre-bump pair; None for a create.
    prior_attempt: AttemptPair | None = None
    prepared: PreparedSpawn | None = None


@dataclass(frozen=True)
class _Outcome:
    value: object = None
    error: BaseException | None = None


async def run_in_doubt_owner(
    attempt: InDoubtAttempt,
    stage: OwnerStage,
    task: asyncio.Future[Any] | None,
) -> None:
    """Own ``attempt`` from the handoff until a confirmed settlement releases it."""
    try:
        outcome = await _drain(task)
        if stage in ("create", "bind", "reserve"):
            await _settle_unprepared(attempt, stage, outcome)
        else:
            await _settle_prepared(attempt, stage, outcome)
    except asyncio.CancelledError:
        logger.warning(
            "In-doubt owner for terminal %s stopped at stage %s with its settlement "
            "pending; restart recovery owns the row",
            attempt.terminal_id,
            stage,
        )
        raise


async def _drain(task: asyncio.Future[Any] | None) -> _Outcome:
    """Await the handed task's real completion; only the owner's own cancel escapes."""
    if task is None:
        return _Outcome()
    try:
        return _Outcome(value=await asyncio.shield(task))
    except asyncio.CancelledError as exc:
        if task.cancelled():
            return _Outcome(error=exc)
        raise
    except Exception as exc:
        return _Outcome(error=exc)


async def _shielded[T](work: Coroutine[Any, Any, T]) -> T:
    """Run ``work`` as its own task so cancelling the owner never abandons it."""
    return await asyncio.shield(asyncio.ensure_future(work))


async def _offload[T](func: Callable[..., T], *args: object, **kwargs: object) -> T:
    return await _shielded(asyncio.to_thread(func, *args, **kwargs))


async def _settle_unprepared(attempt: InDoubtAttempt, stage: OwnerStage, outcome: _Outcome) -> None:
    # No prepare was dispatched, so no host resource can exist: settle the row
    # fail_pending_attempt, recovering it by read-back when create or bump raised.
    if stage == "create" and outcome.error is None:
        row = outcome.value
        if not isinstance(row, Terminal):
            # The bump's CAS matched nothing, so this attempt wrote no row.
            await release_claim(attempt.terminal_id, run_deferred=True)
            return
        attempt.pair = (row.attempt_generation, row.attempt_started_at)

    async def cycle() -> bool:
        if attempt.pair is None:
            decided, pair = _recovered_pair(attempt, await read_row(attempt))
            if not decided or pair is None:
                # Undecided retries the read; a decided None settled with no write.
                return decided
            attempt.pair = pair
        return await _write_confirmed(attempt, "exited")

    await _until_confirmed(attempt, "fail_pending_attempt", cycle)
    await release_claim(attempt.terminal_id, run_deferred=True)


def _recovered_pair(
    attempt: InDoubtAttempt, row: Terminal | None
) -> tuple[bool, AttemptPair | None]:
    """Decide a raised create or bump from the committed row: (decided, pair to settle)."""
    if row is None or row.state == "exited":
        return True, None
    pair = (row.attempt_generation, row.attempt_started_at)
    if attempt.prior_attempt is not None and pair == attempt.prior_attempt:
        # The bump rolled back; the earlier attempt is not this owner's.
        return True, None
    if row.state == "pending" and (
        attempt.prior_attempt is not None or row.spawn_key == attempt.spawn_key
    ):
        return True, pair
    return False, None


async def _settle_prepared(attempt: InDoubtAttempt, stage: OwnerStage, outcome: _Outcome) -> None:
    if stage == "prepare":
        attempt.prepared = outcome.value if isinstance(outcome.value, PreparedSpawn) else None
    else:
        await _orphan_live_row(attempt)
    prepared = attempt.prepared
    if prepared is None:
        await _settle_after_absence(attempt)
        return
    identity = prepared_identity(attempt.backend, prepared)
    if await _kill_with_proof(attempt, prepared):
        await _until_confirmed(attempt, "exited", lambda: _write_confirmed(attempt, "exited"))
        await release_claim(attempt.terminal_id, run_deferred=True)
    elif identity is None:
        await _settle_after_absence(attempt)
    else:
        await _until_confirmed(
            attempt, "orphan", lambda: _write_confirmed(attempt, "orphan", identity)
        )
        # The kept orphan keeps its created isolation, so compensation is dropped.
        await release_claim(attempt.terminal_id, run_deferred=False)


async def _orphan_live_row(attempt: InDoubtAttempt) -> None:
    """Step 1 at stage promote: keep a promoted pane by moving its row to orphaned."""
    pair = _require_pair(attempt)
    for _ in range(IMMEDIATE_RETRIES + 1):
        try:
            async with attempt.manager.settle_lock(attempt.terminal_id):
                row = await read_row_unlocked(attempt)
                if row is None or row.state != "live":
                    if row is not None and row.state != "pending":
                        logger.info(
                            "In-doubt owner found terminal %s %s after promotion",
                            attempt.terminal_id,
                            row.state,
                        )
                    return
                await _offload(
                    attempt.manager.mark_kill_failed,
                    attempt.terminal_id,
                    attempt_generation=pair[0],
                    attempt_started_at=pair[1],
                )
                return
        except Exception as exc:
            logger.warning(
                "In-doubt owner could not orphan terminal %s (%s)",
                attempt.terminal_id,
                type(exc).__name__,
            )
    # The row stays live under the claim; the kill decision still runs.


async def _settle_after_absence(attempt: InDoubtAttempt) -> None:
    """Absence retry: prove the session gone (killing it when present), then settle."""
    await _until_confirmed(attempt, "absence proof", lambda: _prove_absent(attempt))
    await _until_confirmed(
        attempt, "fail_pending_attempt", lambda: _write_confirmed(attempt, "exited")
    )
    await release_claim(attempt.terminal_id, run_deferred=True)


async def _prove_absent(attempt: InDoubtAttempt) -> bool:
    runtime = attempt.runtime
    if attempt.backend == "native":
        find = getattr(runtime, "find_host_terminal", None)
        if not callable(find):
            return False
        host_terminal_id = await _shielded(find(attempt.terminal_id, attempt.spawn_key))
        if host_terminal_id is None:
            return True
        terminate_host_id = getattr(runtime, "terminate_host_id", None)
        if not callable(terminate_host_id):
            return False
        mismatch = await _shielded(terminate_host_id(host_terminal_id, _client_epoch(runtime)))
        return mismatch is None
    from gobby.agents.capture import backend_session_present

    target = await _probe_target(attempt)
    if not await _shielded(backend_session_present(runtime, target)):
        return True
    await _shielded(runtime.terminate(target, KILL_GRACE_SECONDS))
    return not await _shielded(backend_session_present(runtime, target))


async def _probe_target(attempt: InDoubtAttempt) -> Terminal:
    row = await read_row(attempt)
    if row is None:
        raise LookupError(f"terminal {attempt.terminal_id} has no row to probe")
    return replace(row, session_name=attempt.spawn_key)


async def _kill_with_proof(attempt: InDoubtAttempt, prepared: PreparedSpawn) -> bool:
    """Kill through the prepared identity; true only when the kill is proven."""
    from gobby.agents.capture import backend_session_present

    runtime = attempt.runtime
    try:
        row = await read_row(attempt)
        if row is None:
            return False
        target = _kill_target(attempt, row, prepared)
        if attempt.backend == "native":
            expected = target.host_epoch
            before = _client_epoch(runtime)
            await _shielded(runtime.terminate(target, KILL_GRACE_SECONDS))
            if expected and expected == before:
                # The host's kill raises on failure; the epoch must hold across it.
                return _client_epoch(runtime) == expected
            # A stale or missing epoch takes the strict stale branch, which
            # returns only on a proven kill or a proven absence.
            return True
        await _shielded(runtime.terminate(target, KILL_GRACE_SECONDS))
        return not await _shielded(backend_session_present(runtime, target))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "In-doubt owner kill of terminal %s is unproven (%s)",
            attempt.terminal_id,
            type(exc).__name__,
        )
        return False


def _kill_target(attempt: InDoubtAttempt, row: Terminal, prepared: PreparedSpawn) -> Terminal:
    identity = prepared_identity(attempt.backend, prepared)
    if identity is None:
        return replace(row, session_name=attempt.spawn_key)
    process = dict(row.process or {})
    process.update(identity.process or {})
    return replace(
        row,
        locator=dict(identity.locator),
        locator_key=identity.locator_key,
        host_epoch=identity.host_epoch if attempt.backend == "native" else row.host_epoch,
        process=process or None,
        session_name=attempt.spawn_key if attempt.backend == "tmux" else row.session_name,
    )


def prepared_identity(backend: str, prepared: PreparedSpawn) -> OrphanIdentity | None:
    """The identity an orphaned row keeps: the prepared locator, epoch and process."""
    if not prepared.stored_locator or not prepared.locator_key:
        return None
    if backend != "native":
        return OrphanIdentity(locator=prepared.stored_locator, locator_key=prepared.locator_key)
    epoch = prepared.locator.frame_host_epoch if prepared.locator else None
    process: dict[str, object] = {}
    if prepared.host_terminal_id is not None:
        process["host_terminal_id"] = prepared.host_terminal_id
    if prepared.process is not None:
        process.update({"pgid": prepared.process.pgid, "start_time": prepared.process.start_time})
    return OrphanIdentity(
        locator=prepared.stored_locator,
        locator_key=prepared.locator_key,
        host_epoch=epoch or None,
        process=process or None,
    )


def _client_epoch(runtime: TerminalRuntime) -> str:
    return str(getattr(runtime, "host_epoch", "") or "")


async def _write_confirmed(
    attempt: InDoubtAttempt,
    settlement: Settlement,
    identity: OrphanIdentity | None = None,
) -> bool:
    """Issue the one decided settlement under ``settle_lock`` and confirm it by read-back."""
    manager, terminal_id = attempt.manager, attempt.terminal_id
    generation, started_at = _require_pair(attempt)
    pair = {"attempt_generation": generation, "attempt_started_at": started_at}
    async with manager.settle_lock(terminal_id):
        row = await read_row_unlocked(attempt)
        if _confirmed(attempt, row, settlement, identity):
            return True
        if row is None:
            return False
        if settlement == "exited":
            if row.state == "pending":
                await _offload(manager.fail_pending_attempt, terminal_id, **pair)
            elif row.state in {"live", "orphaned"}:
                await _offload(manager.mark_exited_attempt, terminal_id, **pair)
        elif identity is not None:
            if row.state == "pending":
                await _offload(manager.mark_kill_failed, terminal_id, **pair, identity=identity)
            elif row.state == "live":
                await _offload(manager.mark_kill_failed, terminal_id, **pair)
                if identity.host_epoch is not None:
                    await _record_identity(manager, terminal_id, pair, identity)
            elif row.state == "orphaned" and identity.host_epoch is not None:
                await _record_identity(manager, terminal_id, pair, identity)
        return _confirmed(attempt, await read_row_unlocked(attempt), settlement, identity)


async def _record_identity(
    manager: TerminalManager,
    terminal_id: str,
    pair: dict[str, Any],
    identity: OrphanIdentity,
) -> None:
    await _offload(
        manager.record_orphan_identity,
        terminal_id,
        **pair,
        locator=identity.locator,
        locator_key=identity.locator_key,
        host_epoch=identity.host_epoch or "",
        process=identity.process,
    )


def _confirmed(
    attempt: InDoubtAttempt,
    row: Terminal | None,
    settlement: Settlement,
    identity: OrphanIdentity | None,
) -> bool:
    if row is None or (row.attempt_generation, row.attempt_started_at) != attempt.pair:
        return False
    if settlement == "exited":
        return row.state == "exited"
    # The row must carry the whole prepared identity: locator, epoch and every
    # recorded process key (the store keeps unrelated process keys beside them).
    if row.state != "orphaned" or identity is None:
        return False
    process = row.process or {}
    return (
        row.locator_key == identity.locator_key
        and dict(row.locator or {}) == dict(identity.locator)
        and (identity.host_epoch is None or row.host_epoch == identity.host_epoch)
        and all(process.get(key) == value for key, value in (identity.process or {}).items())
    )


async def _until_confirmed(
    attempt: InDoubtAttempt, pending: str, cycle: Callable[[], Awaitable[bool]]
) -> None:
    """Repeat ``cycle`` until it confirms: three immediate retries, then capped backoff."""
    failures = 0
    delay = BACKOFF_START_SECONDS
    last_warning: float | None = None
    while True:
        try:
            if await cycle():
                if failures > IMMEDIATE_RETRIES:
                    logger.info(
                        "In-doubt owner confirmed %s for terminal %s after %d retries",
                        pending,
                        attempt.terminal_id,
                        failures,
                    )
                return
            reason = "unconfirmed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = type(exc).__name__
        failures += 1
        if failures <= IMMEDIATE_RETRIES:
            continue
        now = time.monotonic()
        if last_warning is None or now - last_warning >= WARNING_INTERVAL_SECONDS:
            logger.warning(
                "In-doubt owner retrying %s for terminal %s (%s); the claim stays held",
                pending,
                attempt.terminal_id,
                reason,
            )
            last_warning = now
        await _sleep(delay)
        delay = min(delay * 2, BACKOFF_CAP_SECONDS)


def _require_pair(attempt: InDoubtAttempt) -> AttemptPair:
    if attempt.pair is None:
        raise RuntimeError(f"terminal {attempt.terminal_id} has no committed attempt")
    return attempt.pair


async def read_row(attempt: InDoubtAttempt) -> Terminal | None:
    async with attempt.manager.settle_lock(attempt.terminal_id):
        return await read_row_unlocked(attempt)


async def read_row_unlocked(attempt: InDoubtAttempt) -> Terminal | None:
    return await _offload(attempt.manager.get, attempt.terminal_id)


async def confirm_exited(attempt: InDoubtAttempt) -> bool:
    """True when the row reads back ``exited`` with this attempt's pair."""
    return _confirmed(attempt, await read_row(attempt), "exited", None)


async def release_claim(terminal_id: str, *, run_deferred: bool) -> None:
    """Release after a confirmed settlement and run the returned steps exactly once.

    The released steps have no other owner, so they run shielded: cancelling the
    caller mid-step neither abandons that step nor skips the rest.
    """
    steps = in_doubt_spawns.release(terminal_id)
    if run_deferred and steps:
        await _shielded(_run_deferred(terminal_id, steps))


async def _run_deferred(terminal_id: str, steps: list[DeferredStep]) -> None:
    for step in steps:
        try:
            await step()
        except Exception:
            logger.warning(
                "Deferred spawn compensation failed for terminal %s", terminal_id, exc_info=True
            )
