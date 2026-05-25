from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ClassVar

import pytest

from gobby.storage.hub._ambient import enter_transaction
from gobby.storage.hub.protocol import LockTarget

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _TestLock:
    PRIORITY: ClassVar[int] = 100
    key: str = "test"


class _Transaction:
    def __init__(self, *, is_immediate: bool) -> None:
        self.is_immediate = is_immediate

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
