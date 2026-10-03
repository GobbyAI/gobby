"""Deadline-bounded async PostgreSQL operations on dedicated connections."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Never

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS = 1.0

_CANCEL_GRACE_SECONDS = 0.05

type AsyncDBWork[T] = Callable[[psycopg.AsyncConnection[Any], float], Awaitable[T]]


class BoundedDBTimeoutError(TimeoutError):
    """The work deadline expired before COMMIT submission, so no change committed."""


class IndeterminateCommitError(RuntimeError):
    """COMMIT was submitted, but its final server outcome was not observed."""


class CommittedCleanupError(RuntimeError):
    """COMMIT was observed, but terminating or reaping the connection failed."""

    def __init__(self, message: str = "", *, result: Any = None) -> None:
        super().__init__(message)
        self.result = result


class _WorkBudgetExpired(Exception):
    """Internal signal raised before COMMIT when the work budget is exhausted."""


@dataclass
class _RunState:
    connection: psycopg.AsyncConnection[Any] | None = None
    commit_submitted: bool = False
    commit_observed: bool = False
    result: Any = None
    caller_cancellation: asyncio.CancelledError | None = None


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
        # COMMIT of an aborted transaction silently rolls back (#23296).
        if connection.info.transaction_status == TransactionStatus.INERROR:
            raise psycopg.errors.InFailedSqlTransaction(
                "transaction aborted by a swallowed statement error"
            )

        state.commit_submitted = True
        await connection.commit()
        state.commit_observed = True
        state.result = result
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
    termination_error: BaseException | None = None
    child.cancel()
    grace = min(_CANCEL_GRACE_SECONDS, _remaining(cleanup_deadline))
    if grace > 0.0:
        try:
            done, _ = await asyncio.wait({child}, timeout=grace)
            if done:
                return _consume_child_result(child)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                state.caller_cancellation = state.caller_cancellation or exc
            termination_error = exc

    connection = state.connection
    if connection is not None:
        for _ in range(2):
            try:
                connection.pgconn.finish()
            except BaseException as exc:
                termination_error = termination_error or exc
            else:
                break
    child.cancel()

    while not child.done() and (reap_budget := _remaining(cleanup_deadline)) > 0.0:
        try:
            await asyncio.wait({child}, timeout=reap_budget)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                state.caller_cancellation = state.caller_cancellation or exc
            termination_error = termination_error or exc
            child.cancel()

    if child.done():
        child_error = _consume_child_result(child)
        if termination_error is not None:
            raise termination_error
        return child_error
    raise RuntimeError(
        "bounded PostgreSQL child ignored terminal cancellation"
    ) from termination_error


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
    except BaseException as exc:
        if state.commit_submitted and not state.commit_observed:
            _raise_indeterminate(exc)
        if state.commit_observed:
            raise CommittedCleanupError(
                "COMMIT was observed, but cleanup failed; the change is durable",
                result=state.result,
            ) from exc
        if isinstance(
            exc, (_WorkBudgetExpired, psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable)
        ):
            _raise_timeout(exc)
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
    After observed COMMIT, caller cancellation propagates with the durable result
    in a ``CommittedCleanupError`` cause; clean deadline completion returns it.
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

    cancellation: asyncio.CancelledError | None = None
    try:
        done, _ = await asyncio.wait({child}, timeout=_remaining(work_cutoff))
    except asyncio.CancelledError as exc:
        cancellation = exc
        cleanup_deadline = min(
            caller_deadline,
            loop.time() + RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS,
        )
    else:
        if done:
            return _result_or_raise(child, state)
        cleanup_deadline = caller_deadline

    try:
        child_error = await _terminate_child(child, state, cleanup_deadline=cleanup_deadline)
    except BaseException as exc:
        if not state.commit_submitted:
            raise
        child_error = exc
    cancellation = cancellation or state.caller_cancellation
    if state.commit_submitted and not state.commit_observed:
        _raise_indeterminate(child_error or cancellation)
    if state.commit_observed:
        if child_error is not None or cancellation is not None:
            committed = CommittedCleanupError(
                "COMMIT was observed, but cleanup was interrupted or failed; "
                "the change is durable; resume interrupted follow-up using this result",
                result=state.result,
            )
            # The same caller exception cannot be its own transitive cause.
            committed.__cause__ = child_error if child_error is not cancellation else None
            if cancellation is not None:
                raise cancellation from committed
            raise committed from child_error
        return child.result()
    if cancellation is not None:
        raise cancellation
    _raise_timeout(child_error)
