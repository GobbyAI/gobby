"""Live-lease admission and in-transaction epoch fencing for effectful writes."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase, Transaction

logger = logging.getLogger(__name__)

LEASE_LOSS_DRAIN_TIMEOUT_SECONDS = 30.0
_bound_writers: WeakKeyDictionary[object, Callable[[Callable[[Transaction], None]], None]] = (
    WeakKeyDictionary()
)


class LeaseNotHeld(RuntimeError):
    """Effectful work was refused because this process does not own the live lease."""

    code = "lease_not_held"

    def __init__(self, message: str = "active-daemon lease is not live") -> None:
        super().__init__(message)
        self.message = message


class StaleEpochFence(RuntimeError):
    """A hub write was refused because the owned fencing epoch is no longer current."""

    code = "stale_epoch"

    def __init__(self, message: str = "owned fencing epoch is stale") -> None:
        super().__init__(message)
        self.message = message


class EffectFence:
    """In-process drain gate for predecessor work during takeover."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._in_flight = 0
        self._serving = True

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def serving(self) -> bool:
        with self._lock:
            return self._serving

    def admit(self) -> _Admission:
        return _Admission(self)

    def drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            self._serving = False
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out draining predecessor in-flight work")
                self._idle.wait(timeout=remaining)

    def resume(self) -> None:
        with self._lock:
            self._serving = True


class _Admission:
    def __init__(self, fence: EffectFence) -> None:
        self._fence = fence

    def __enter__(self) -> EffectFence:
        with self._fence._lock:
            if not self._fence._serving:
                raise LeaseNotHeld("predecessor in-flight work has been drained")
            self._fence._in_flight += 1
        return self._fence

    def __exit__(self, *_exc: object) -> None:
        with self._fence._lock:
            self._fence._in_flight -= 1
            if self._fence._in_flight == 0:
                self._fence._idle.notify_all()


async def await_test_admit_barrier() -> None:
    """Yield after admission when an isolated e2e barrier file is present."""
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        return
    home = os.environ.get("GOBBY_HOME")
    if not home:
        return
    flag = Path(home) / "runtime" / "e2e-admit-barrier"
    if not flag.is_file():
        return
    admitted = flag.with_name("e2e-admit-barrier.admitted")
    release = flag.with_name("e2e-admit-barrier.release")
    admitted.write_text("1")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if release.is_file():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("e2e admit barrier was not released")


def owns_live_lease(lease: object | None) -> bool:
    """In-memory ownership check: lease session alive and cached epoch present."""
    if lease is None:
        return False
    checker = getattr(lease, "owns_live_lease", None)
    if callable(checker):
        return bool(checker())
    acquired = getattr(lease, "acquired", False)
    epoch = getattr(lease, "fencing_epoch", None)
    return bool(acquired) and epoch is not None


def fenced_hub_write(
    db: HubDatabase,
    *,
    deployment_token: str,
    owned_epoch: int,
    writer: Callable[[Transaction], None],
) -> None:
    """Validate the owned epoch inside the same transaction as ``writer``."""
    with db.transaction() as txn:
        row = txn.execute(
            """
            SELECT fencing_epoch
              FROM deployment_runtime
             WHERE deployment_token = %s
             FOR UPDATE
            """,
            (deployment_token,),
        ).fetchone()
        current = None if row is None else int(row["fencing_epoch"])
        if current != owned_epoch:
            raise StaleEpochFence(
                f"owned fencing epoch {owned_epoch} does not match deployment_runtime"
            )
        writer(txn)


def bind_fenced_writer(db: object, lease: object) -> None:
    """Register a production fenced writer that reads the live lease epoch."""

    def run_fenced_write(writer: Callable[[Transaction], None]) -> None:
        epoch = getattr(lease, "fencing_epoch", None)
        token = getattr(lease, "deployment_token", None)
        if epoch is None or not token:
            raise StaleEpochFence("active-daemon lease has no fencing epoch")
        fenced_hub_write(
            cast("HubDatabase", db),
            deployment_token=str(token),
            owned_epoch=int(epoch),
            writer=writer,
        )

    _bound_writers[db] = run_fenced_write


def run_hub_mutation(db: HubDatabase, writer: Callable[[Transaction], None]) -> None:
    """Run ``writer`` inside a fenced hub transaction when a writer is bound."""
    fenced = _bound_writers.get(db)
    if fenced is not None:
        fenced(writer)
        return
    with db.transaction() as txn:
        writer(txn)


def drain_effect_fence(
    fence: EffectFence | None,
    *,
    timeout: float = LEASE_LOSS_DRAIN_TIMEOUT_SECONDS,
) -> None:
    """Stop new admissions and wait for in-flight handlers after lease loss."""
    if fence is None:
        return
    try:
        fence.drain(timeout=timeout)
    except TimeoutError:
        logger.error("timed out draining predecessor in-flight work after lease loss")
