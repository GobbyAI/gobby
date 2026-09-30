"""Request-scoped hub query and pool-acquire timing callbacks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_query_observer: ContextVar[Callable[[float], None] | None] = ContextVar(
    "hub_query_observer", default=None
)
_pool_acquire_observer: ContextVar[Callable[[float], None] | None] = ContextVar(
    "hub_pool_acquire_observer", default=None
)


@contextmanager
def observe_queries(
    observer: Callable[[float], None],
    *,
    pool_acquire_observer: Callable[[float], None] | None = None,
) -> Iterator[None]:
    query_token = _query_observer.set(observer)
    acquire_token = _pool_acquire_observer.set(pool_acquire_observer)
    try:
        yield
    finally:
        _pool_acquire_observer.reset(acquire_token)
        _query_observer.reset(query_token)


def record_query(duration_seconds: float) -> None:
    observer = _query_observer.get()
    if observer is not None:
        observer(duration_seconds)


def record_pool_acquire(duration_seconds: float) -> None:
    """Report time spent waiting for a pooled connection, which query timing excludes."""
    observer = _pool_acquire_observer.get()
    if observer is not None:
        observer(duration_seconds)
