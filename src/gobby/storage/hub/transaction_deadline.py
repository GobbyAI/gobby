"""Refresh PostgreSQL bounds at each operation in an owned transaction."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from weakref import WeakKeyDictionary

import psycopg

from gobby.storage.hub.operation_deadline import (
    DatabaseOperationDeadlineExceeded,
    current_database_operation_deadline,
)

# Session (not SET LOCAL) statement and lock timeouts per pooled connection.
_SESSION_TIMEOUTS: WeakKeyDictionary[psycopg.Connection[Any], tuple[int, int]] = WeakKeyDictionary()
_TIMEOUT_MENTION = re.compile(
    r"\b(?:set|reset)\s+(?:(?:local|session)\s+)?\w*timeout"
    r"|\bset_config\s*\(\s*'\w*timeout'"
    r"|\b(?:reset|discard)\s+all\b",
    re.IGNORECASE,
)


def _milliseconds(value: str) -> int:
    """Parse PostgreSQL's normalized SHOW output (all timeout GUCs use ms)."""
    match = re.fullmatch(r"(\d+)(ms|s|min|h|d)?", value)
    if match is None:
        raise ValueError(f"Unexpected PostgreSQL timeout setting: {value}")
    unit = {None: 1, "ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(match[1]) * unit[match[2]]


class TransactionDeadline:
    """Keep the transaction owner and honor narrower ambient scopes.

    PostgreSQL restarts statement_timeout for every statement. Reinstall the
    remaining budget before each operation, including COMMIT. A scope introduced
    inside an existing transaction is read here too; restore its settings before
    the next operation outside that scope. SET LOCAL prevents pool leaks.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn
        self._owner = current_database_operation_deadline()
        self._original_timeouts: tuple[int, int] | None = None
        self._bounds: list[tuple[int, int]] = []
        self._timeouts_touched = False

    def observe(self, sql: str) -> None:
        """Note an executed statement that may have set a timeout itself.

        The session baseline is cached per connection. A transaction that set
        a timeout (SET LOCAL, a session SET or set_config) reads it with SHOW
        from then on, and a session change must be read again next time.
        """
        if _TIMEOUT_MENTION.search(sql) is not None:
            self._timeouts_touched = True
            _SESSION_TIMEOUTS.pop(self._conn, None)

    @property
    def active(self) -> bool:
        return (
            bool(self._bounds)
            or self._owner is not None
            or current_database_operation_deadline() is not None
        )

    def savepoint_state(self) -> tuple[int, int] | None:
        """Capture the baseline corresponding to PostgreSQL's savepoint GUC state."""
        return self._original_timeouts

    def restore_savepoint_state(self, state: tuple[int, int] | None) -> None:
        # ROLLBACK TO restores SET LOCAL too. Retain the matching baseline so
        # prepare() can replace a restored bound whose Python scope has ended.
        self._original_timeouts = state

    @contextmanager
    def bounds(self, statement_ms: int, lock_ms: int) -> Iterator[None]:
        """Compose explicit transaction caps with the workflow's shrinking budget."""
        self._bounds.append((statement_ms, lock_ms))
        try:
            self.prepare()
            yield
        except BaseException:
            self._bounds.pop()
            try:
                self.prepare()
            except (DatabaseOperationDeadlineExceeded, psycopg.Error):
                # Preserve the body error. An aborted transaction needs rollback;
                # an expired owner cannot submit another configuration statement.
                pass
            raise
        else:
            self._bounds.pop()
            self.prepare()

    def prepare(self) -> None:
        deadlines = [
            deadline
            for deadline in (self._owner, current_database_operation_deadline())
            if deadline is not None
        ]
        if not deadlines and not self._bounds:
            if self._original_timeouts is not None:
                self._set_timeouts(self._original_timeouts)
                self._original_timeouts = None
            return

        # Check both before and after the configuration round trip.
        for deadline in deadlines:
            deadline.remaining_seconds()
        if self._original_timeouts is None:
            self._original_timeouts = self._baseline_timeouts()
        limits = list(self._bounds)
        if deadlines:
            remaining_ms = max(1, math.floor(min(d.remaining_seconds() for d in deadlines) * 1000))
            limits.append((remaining_ms, remaining_ms))
        self._set_timeouts(
            tuple(
                min([cap[i] for cap in limits] + ([value] if value else []))
                for i, value in enumerate(self._original_timeouts)
            )
        )
        for deadline in deadlines:
            deadline.remaining_seconds()

    def _baseline_timeouts(self) -> tuple[int, int]:
        # Each deadline owns its pooled connection's whole transaction, so while
        # untouched the settings outside our SET LOCAL are the session's, which
        # only a statement observe() flags can change: the SHOW pair cost ~125 us
        # of GIL time per deadline transaction (#23359).
        if not self._timeouts_touched:
            cached = _SESSION_TIMEOUTS.get(self._conn)
            if cached is not None:
                return cached
        # SHOW/SET are utility commands: unlike SELECT, they allow callers
        # to choose transaction isolation before the first data query.
        statement = self._conn.execute("SHOW statement_timeout").fetchone()
        lock = self._conn.execute("SHOW lock_timeout").fetchone()
        assert statement is not None and lock is not None
        timeouts = (
            _milliseconds(statement["statement_timeout"]),
            _milliseconds(lock["lock_timeout"]),
        )
        if not self._timeouts_touched:
            _SESSION_TIMEOUTS[self._conn] = timeouts
        return timeouts

    def _set_timeouts(self, values: tuple[int, ...]) -> None:
        # PostgreSQL timeout GUCs use signed 32-bit milliseconds. A distant
        # application deadline must not overflow the server setting.
        values = tuple(min(value, 2**31 - 1) for value in values)
        self._conn.execute(
            f"SET LOCAL statement_timeout = '{values[0]}ms'; "
            f"SET LOCAL lock_timeout = '{values[1]}ms'"
        )
