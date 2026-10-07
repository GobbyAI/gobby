"""Per-hook-event memo for repeated hub reads (#23721).

One hook event reads the same session row through several unrelated
collaborators. Inside ``hub_read_scope()`` a keyed read is served from memory
until a transaction in this process writes one of the tables it depends on.
Writes are stamped when their transaction closes, so a write from another
thread or event loop also invalidates the memo. Reads inside an open
transaction always reach the database, keeping lock-holding read-modify-write
code on committed state.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, cast

from gobby.storage.hub._ambient import ambient_transaction

# Readers of the same row share one key, e.g. ("session_variables", session_id).
SESSION_VARIABLES_TABLES = frozenset({"session_variables"})

_READ_PREFIXES = ("SELECT", "SHOW", "SET ", "SAVEPOINT", "RELEASE")
_WRITE_TARGET = re.compile(r"\b(?:UPDATE|INSERT\s+INTO)\s+(?:ONLY\s+)?\"?([A-Za-z_][\w.]*)", re.I)
_UNSCOPED_WRITE = re.compile(r"\b(?:DELETE|TRUNCATE|MERGE|CREATE|DROP|ALTER|COPY)\b", re.I)

# A process-wide logical clock: each table keeps the tick of its newest
# closed write, and ``_ALL_TABLES`` covers writes whose targets are unknown.
_ALL_TABLES = "*"
_clock = itertools.count(1)
_last_write: dict[str, int] = {}


class _ReadMemo:
    __slots__ = ("active", "entries")

    def __init__(self) -> None:
        self.active = True
        self.entries: dict[Hashable, tuple[frozenset[str], int, Any]] = {}


_SCOPE: ContextVar[_ReadMemo | None] = ContextVar("hub_read_scope", default=None)


@contextmanager
def hub_read_scope() -> Iterator[None]:
    """Memoize keyed hub reads for one hook event."""
    memo = _ReadMemo()
    token = _SCOPE.set(memo)
    try:
        yield
    finally:
        # Tasks spawned during the event keep a copy of this context.
        memo.active = False
        memo.entries.clear()
        _SCOPE.reset(token)


def written_tables(sql: str) -> frozenset[str] | None:
    """Tables one statement writes: empty for reads, None when unknown."""
    head = sql.lstrip()[:9].upper()
    if head.startswith(_READ_PREFIXES):
        return frozenset()
    if _UNSCOPED_WRITE.search(sql):
        return None
    targets = frozenset(m.rsplit(".", 1)[-1].lower() for m in _WRITE_TARGET.findall(sql))
    if targets:
        return targets
    return frozenset() if head.startswith("WITH") else None


def record_closed_writes(tables: frozenset[str] | None) -> None:
    """Stamp the tables a just-closed transaction wrote (None means all)."""
    tick = next(_clock)
    for table in (_ALL_TABLES,) if tables is None else tables:
        _last_write[table] = tick


def _fresh(tables: frozenset[str], loaded_at: int) -> bool:
    return all(_last_write.get(table, 0) < loaded_at for table in (_ALL_TABLES, *tables))


def scoped_read[T](db: object, key: Hashable, tables: frozenset[str], load: Callable[[], T]) -> T:
    """Return ``load()``, reusing this event's earlier result while it is fresh."""
    memo = _SCOPE.get()
    if memo is None or not memo.active or ambient_transaction(db) is not None:
        return load()
    hit = memo.entries.get(key)
    if hit is not None and _fresh(hit[0], hit[1]):
        return cast(T, deepcopy(hit[2]))
    loaded_at = next(_clock)
    value = load()
    memo.entries[key] = (tables, loaded_at, deepcopy(value))
    return value
