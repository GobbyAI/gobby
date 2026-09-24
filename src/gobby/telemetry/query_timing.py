"""Request-scoped hub query timing callback."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_query_observer: ContextVar[Callable[[float], None] | None] = ContextVar(
    "hub_query_observer", default=None
)


@contextmanager
def observe_queries(observer: Callable[[float], None]) -> Iterator[None]:
    token = _query_observer.set(observer)
    try:
        yield
    finally:
        _query_observer.reset(token)


def record_query(duration_seconds: float) -> None:
    observer = _query_observer.get()
    if observer is not None:
        observer(duration_seconds)
