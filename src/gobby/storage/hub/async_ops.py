"""Deadline-bounded async PostgreSQL operations on dedicated connections."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Never

import psycopg
from psycopg import sql

RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS = 1.0

_CANCEL_GRACE_SECONDS = 0.05

type AsyncDBWork[T] = Callable[[psycopg.AsyncConnection[Any], float], Awaitable[T]]


class BoundedDBTimeoutError(TimeoutError):
    """The work deadline expired before COMMIT submission, so no change committed."""


class IndeterminateCommitError(RuntimeError):
    """COMMIT was submitted, but its final server outcome was not observed."""


class _WorkBudgetExpired(Exception):
    """Internal signal raised before COMMIT when the work budget is exhausted."""


@dataclass
class _RunState:
    connection: psycopg.AsyncConnection[Any] | None = None
    commit_submitted: bool = False
    commit_observed: bool = False


def _remaining(cutoff: float) -> float:
    return max(0.0, cutoff - asyncio.get_running_loop().time())


def _require_remaining(cutoff: float) -> float:
    remaining = _remaining(cutoff)
    if remaining <= 0.0:
        raise _WorkBudgetExpired
    return remaining


def _timeout_milliseconds(remaining: float) -> int:
    return max(1, math.floor(remaining * 1000.0))


async def _run_child[T](
    work: AsyncDBWork[T],
    *,
    conninfo: str,
    work_cutoff: float,
    statement_timeout_remaining: bool,
    lock_timeout: bool,
    state: _RunState,
) -> T:
    connection: psycopg.AsyncConnection[Any] | None = None
    try:
        connect_budget = _require_remaining(work_cutoff)
        connection = await psycopg.AsyncConnection.connect(
            conninfo,
            connect_timeout=max(1, math.ceil(connect_budget)),
            prepare_threshold=None,
        )
        state.connection = connection

        if statement_timeout_remaining:
            timeout_ms = _timeout_milliseconds(_require_remaining(work_cutoff))
            await connection.execute(
                sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_ms))
            )
        if lock_timeout:
            timeout_ms = _timeout_milliseconds(_require_remaining(work_cutoff))
            await connection.execute(
                sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(timeout_ms))
            )

        result = await work(connection, _require_remaining(work_cutoff))
        _require_remaining(work_cutoff)

        state.commit_submitted = True
        await connection.commit()
        state.commit_observed = True
        return result
    finally:
        if connection is not None:
            await connection.close()


def _consume_child_result[T](child: asyncio.Task[T]) -> BaseException | None:
    try:
        child.result()
    except BaseException as exc:
        return exc
    return None


async def _terminate_child[T](
    child: asyncio.Task[T],
    state: _RunState,
    *,
    cleanup_deadline: float,
) -> BaseException | None:
    child.cancel()
    grace = min(_CANCEL_GRACE_SECONDS, _remaining(cleanup_deadline))
    if grace > 0.0:
        done, _ = await asyncio.wait({child}, timeout=grace)
        if done:
            return _consume_child_result(child)

    connection = state.connection
    if connection is not None:
        connection.pgconn.finish()
    child.cancel()

    reap_budget = _remaining(cleanup_deadline)
    if reap_budget > 0.0:
        done, _ = await asyncio.wait({child}, timeout=reap_budget)
        if done:
            return _consume_child_result(child)

    if child.done():
        return _consume_child_result(child)
    raise RuntimeError("bounded PostgreSQL child ignored terminal cancellation")


def _raise_timeout(cause: BaseException | None = None) -> Never:
    error = BoundedDBTimeoutError("bounded PostgreSQL work deadline expired")
    if cause is None:
        raise error
    raise error from cause


def _raise_indeterminate(cause: BaseException | None = None) -> Never:
    error = IndeterminateCommitError(
        "PostgreSQL COMMIT was submitted but its outcome could not be observed"
    )
    if cause is None:
        raise error
    raise error from cause


def _result_or_raise[T](child: asyncio.Task[T], state: _RunState) -> T:
    try:
        return child.result()
    except _WorkBudgetExpired as exc:
        _raise_timeout(exc)
    except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as exc:
        if state.commit_submitted and not state.commit_observed:
            _raise_indeterminate(exc)
        _raise_timeout(exc)
    except BaseException as exc:
        if state.commit_submitted and not state.commit_observed:
            _raise_indeterminate(exc)
        raise


async def run_bounded_db[T](
    work: AsyncDBWork[T],
    *,
    conninfo: str,
    deadline_seconds: float,
    statement_timeout_remaining: bool = True,
    lock_timeout: bool = False,
) -> T:
    """Run one dedicated async transaction inside an end-to-end deadline.

    The final second is reserved for cancellation, local socket close, a second
    cancellation that interrupts psycopg's cancel connection, and child reap.
    Timeouts before COMMIT submission are deterministically rolled back by local
    connection close. Failure after submission raises ``IndeterminateCommitError``.
    """
    if deadline_seconds <= 0.0:
        raise ValueError("deadline_seconds must be positive")
    if deadline_seconds <= RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS:
        _raise_timeout()

    loop = asyncio.get_running_loop()
    started = loop.time()
    caller_deadline = started + deadline_seconds
    work_cutoff = caller_deadline - RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS
    state = _RunState()
    child = asyncio.create_task(
        _run_child(
            work,
            conninfo=conninfo,
            work_cutoff=work_cutoff,
            statement_timeout_remaining=statement_timeout_remaining,
            lock_timeout=lock_timeout,
            state=state,
        ),
        name="gobby-bounded-postgres-operation",
    )

    try:
        done, _ = await asyncio.wait({child}, timeout=_remaining(work_cutoff))
    except asyncio.CancelledError as cancellation:
        cleanup_deadline = min(
            caller_deadline,
            loop.time() + RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS,
        )
        child_error = await _terminate_child(
            child,
            state,
            cleanup_deadline=cleanup_deadline,
        )
        if state.commit_submitted and not state.commit_observed:
            _raise_indeterminate(child_error or cancellation)
        raise

    if done:
        return _result_or_raise(child, state)

    child_error = await _terminate_child(
        child,
        state,
        cleanup_deadline=caller_deadline,
    )
    if state.commit_submitted and not state.commit_observed:
        _raise_indeterminate(child_error)
    _raise_timeout(child_error)
