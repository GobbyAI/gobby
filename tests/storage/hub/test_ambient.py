from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from typing import ClassVar

import pytest

from gobby.storage.hub._ambient import ambient_transaction, enter_transaction
from gobby.storage.hub.protocol import LockTarget

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _TestLock:
    PRIORITY: ClassVar[int] = 100
    key: str = "test"


class _Transaction:
    def __init__(self, *, is_immediate: bool) -> None:
        self.is_immediate = is_immediate
        self.closed = False

    def acquire_additional_lock(self, lock: LockTarget) -> None:
        raise AssertionError(f"unexpected additional lock: {lock}")


@contextmanager
def _open_transaction(*, immediate: bool, lock: LockTarget | None) -> Iterator[_Transaction]:
    _ = lock
    yield _Transaction(is_immediate=immediate)


def test_enter_transaction_rejects_immediate_inside_non_immediate() -> None:
    adapter = object()

    with enter_transaction(adapter, _open_transaction, immediate=False):
        with pytest.raises(RuntimeError, match="non-immediate"):
            with enter_transaction(adapter, _open_transaction, immediate=True, lock=_TestLock()):
                pass


def test_copied_context_does_not_reuse_closed_transaction() -> None:
    adapter = object()

    with enter_transaction(adapter, _open_transaction) as transaction:
        inherited_context = copy_context()

    transaction.closed = True
    opened: list[_Transaction] = []

    @contextmanager
    def capture_transaction(*, immediate: bool, lock: LockTarget | None) -> Iterator[_Transaction]:
        _ = lock
        fresh = _Transaction(is_immediate=immediate)
        opened.append(fresh)
        yield fresh

    assert inherited_context.run(ambient_transaction, adapter) is None

    def enter_fresh_transaction() -> None:
        with enter_transaction(adapter, capture_transaction) as fresh_transaction:
            assert fresh_transaction is opened[0]
            assert fresh_transaction is not transaction

    inherited_context.run(enter_fresh_transaction)
