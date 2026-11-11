"""Operator principal marker shared by the HTTP and MCP rule write paths.

Which sessions may change rules is decided by capability rules over agent
definitions; this marker only tells the reserved-variable writer guard that an
authenticated operator HTTP call is in progress.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_operator_rule_changes: ContextVar[bool] = ContextVar("operator_rule_changes", default=False)


@contextmanager
def operator_rule_changes() -> Iterator[None]:
    """Used only after HTTP authentication proves an operator principal."""
    token = _operator_rule_changes.set(True)
    try:
        yield
    finally:
        _operator_rule_changes.reset(token)


def is_operator_rule_change() -> bool:
    """Whether the current call runs under an authenticated operator principal."""
    return _operator_rule_changes.get()
