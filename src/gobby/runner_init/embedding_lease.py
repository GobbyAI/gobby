"""Serving-lease lifecycle for managed embedding runtime bundles."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from gobby.ai.embedding_switch import CompletedSwitchRecord
from gobby.storage.embedding_generation_state import (
    EmbeddingGenerationLeaseExpired,
    EmbeddingGenerationLeaseLost,
    EmbeddingGenerationLeaseRenewTransient,
    EmbeddingServingLease,
)

logger = logging.getLogger(__name__)

_EMBEDDING_LEASE_RENEW_SECONDS = 10.0
_EMBEDDING_RENEW_BACKOFF_INITIAL_SECONDS = 1.0
_EMBEDDING_RENEW_BACKOFF_CAP_SECONDS = 5.0
_EMBEDDING_REACQUIRE_POLL_SECONDS = 5.0
_EMBEDDING_LEASE_RENEW_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="gobby-embedding-lease-renew",
)


class _ManagedEmbeddingLease:
    """Activate, renew, re-acquire, and fence one runtime bundle's serving lease."""

    def __init__(
        self,
        lease: EmbeddingServingLease,
        loop: asyncio.AbstractEventLoop,
        *,
        read_completed_record: Callable[[], CompletedSwitchRecord | None],
        request_rebuild: Callable[[CompletedSwitchRecord | None], None],
        request_projection_repair: Callable[[], None] | None = None,
    ) -> None:
        self.lease = lease
        self.loop = loop
        self.read_completed_record = read_completed_record
        self.request_rebuild = request_rebuild
        self.request_projection_repair = request_projection_repair
        self.renewal_stop = threading.Event()
        self.renewal: Future[None] | None = None

    def assert_serving(self) -> None:
        self.lease.assert_serving()

    def activate(self) -> None:
        self.lease.activate()
        self.renewal_stop.clear()
        self.renewal = _EMBEDDING_LEASE_RENEW_EXECUTOR.submit(_renew_embedding_lease, self)

    def dispose(self) -> None:
        self.renewal_stop.set()
        self.lease.fence()
        if self.renewal is not None:
            self.renewal.cancel()


def _renew_embedding_lease(handle: _ManagedEmbeddingLease) -> None:
    while not handle.renewal_stop.wait(_EMBEDDING_LEASE_RENEW_SECONDS):
        try:
            renewed = _renew_with_backoff(handle.lease, stop_event=handle.renewal_stop)
        except EmbeddingGenerationLeaseLost:
            handle.lease.fence()
            logger.warning(
                "Managed embedding generation lease was lost; attempting re-acquisition",
                extra={
                    "expected_generation": handle.lease.generation,
                    "expected_revision": handle.lease.revision,
                },
            )
            if handle.renewal_stop.is_set():
                return
            if not _reacquire_lease_from_renewal_thread(handle):
                return
            continue
        if handle.renewal_stop.is_set():
            return
        if renewed:
            continue
        if not _reacquire_lease_from_renewal_thread(handle):
            return


def _renew_with_backoff(
    lease: EmbeddingServingLease,
    *,
    stop_event: threading.Event | None = None,
) -> bool:
    """Renew once, retrying transient failures until the local deadline nears."""
    backoff = _EMBEDDING_RENEW_BACKOFF_INITIAL_SECONDS
    attempt = 0
    while True:
        attempt += 1
        started_at = time.monotonic()
        remaining_before_attempt = lease.remaining_seconds()
        try:
            lease.renew()
            elapsed_ms = (time.monotonic() - started_at) * 1000.0
            if elapsed_ms >= 1000.0:
                # Diagnostic breadcrumb only: a slow success is never actionable,
                # and the failure/fence paths below warn on their own.
                logger.debug(
                    "Embedding generation lease renewal completed slowly "
                    "attempt=%d elapsed_ms=%.1f remaining_lease_seconds=%.3f",
                    attempt,
                    elapsed_ms,
                    lease.remaining_seconds(),
                )
            return True
        except EmbeddingGenerationLeaseExpired:
            logger.warning(
                "Embedding generation lease renewal attempt reached local deadline "
                "attempt=%d elapsed_ms=%.1f remaining_before_attempt=%.3f",
                attempt,
                (time.monotonic() - started_at) * 1000.0,
                remaining_before_attempt,
            )
            raise
        except EmbeddingGenerationLeaseRenewTransient as exc:
            remaining_seconds = lease.remaining_seconds()
            cause = exc.__cause__ or exc
            logger.warning(
                "Embedding generation lease renewal failed transiently "
                "attempt=%d elapsed_ms=%.1f remaining_lease_seconds=%.3f cause=%s: %s",
                attempt,
                (time.monotonic() - started_at) * 1000.0,
                remaining_seconds,
                type(cause).__name__,
                cause,
            )
            if remaining_seconds <= backoff:
                lease.fence()
                logger.warning(
                    "Embedding generation lease renewal kept failing transiently; serving fenced"
                )
                return False
            if stop_event is None:
                time.sleep(backoff)
            elif stop_event.wait(backoff):
                return True
            backoff = min(backoff * 2.0, _EMBEDDING_RENEW_BACKOFF_CAP_SECONDS)


def _reacquire_lease_from_renewal_thread(handle: _ManagedEmbeddingLease) -> bool:
    reacquisition = asyncio.run_coroutine_threadsafe(_reacquire_lease(handle), handle.loop)
    while True:
        try:
            return reacquisition.result(timeout=0.1)
        except FutureTimeoutError:
            if not handle.renewal_stop.is_set():
                continue
            reacquisition.cancel()
            return False
        except CancelledError:
            return False


async def _reacquire_lease(handle: _ManagedEmbeddingLease) -> bool:
    """Re-acknowledge a matching lease after connectivity returns, or rebuild."""
    while True:
        await asyncio.sleep(_EMBEDDING_REACQUIRE_POLL_SECONDS)
        if handle.renewal_stop.is_set():
            return False
        try:
            record = await asyncio.to_thread(handle.read_completed_record)
        except Exception:
            logger.debug(
                "Embedding lease re-acquisition probe failed; storage still unreachable",
                exc_info=True,
            )
            continue
        if handle.renewal_stop.is_set():
            return False
        if not _lease_matches_record(handle.lease, record):
            logger.warning(
                "Embedding generation changed while serving was fenced; requesting memory "
                "services rebuild expected_generation=%s expected_revision=%d "
                "observed_generation=%s observed_revision=%s decision=rebuild",
                handle.lease.generation,
                handle.lease.revision,
                record.run_id if record is not None else None,
                record.committed_revision if record is not None else None,
            )
            handle.request_rebuild(record)
            return False
        successor = handle.lease.successor()
        try:
            await asyncio.to_thread(successor.activate)
        except EmbeddingGenerationLeaseLost:
            logger.warning(
                "Embedding lease re-acknowledgement lost to a newer serving lease "
                "expected_generation=%s expected_revision=%d decision=rebuild",
                handle.lease.generation,
                handle.lease.revision,
            )
            handle.request_rebuild(record)
            return False
        except Exception:
            logger.debug("Embedding lease re-acknowledgement failed", exc_info=True)
            continue
        if handle.renewal_stop.is_set():
            successor.fence()
            return False
        handle.lease = successor
        logger.info(
            "Embedding generation serving lease re-acknowledged generation=%s revision=%d "
            "decision=resume",
            successor.generation,
            successor.revision,
        )
        if handle.request_projection_repair is not None:
            try:
                handle.request_projection_repair()
            except Exception:
                logger.warning(
                    "Embedding projection repair failed after lease re-acknowledgement "
                    "generation=%s revision=%d; successor lease remains serving",
                    successor.generation,
                    successor.revision,
                    exc_info=True,
                )
        return True


def _lease_matches_record(
    lease: EmbeddingServingLease,
    record: CompletedSwitchRecord | None,
) -> bool:
    """Use managed proof when present; activation authoritatively fences unmanaged leases."""
    return record is None or (
        record.run_id == lease.generation and record.committed_revision == lease.revision
    )
