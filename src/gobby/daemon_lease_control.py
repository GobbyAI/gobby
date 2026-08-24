"""Minimal HTTP control plane for a standby Gobby daemon."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

import uvicorn
from fastapi import FastAPI, Header, HTTPException

from gobby.daemon_lease import (
    DaemonLeaseError,
    DaemonLeaseStatus,
    FreshLeaseOwnerError,
    LeaseConnectionLostError,
    LeaseHeartbeatAbort,
)

logger = logging.getLogger(__name__)


class LeaseBackend(Protocol):
    """Lease operations used by the standby control plane."""

    def status(self) -> DaemonLeaseStatus: ...

    def try_acquire(self) -> bool: ...

    def heartbeat(self) -> None: ...

    def abort_heartbeat(
        self,
        *,
        on_invalidated: Callable[[], None],
        cancel_timeout_seconds: float = 1.0,
    ) -> LeaseHeartbeatAbort: ...

    def close_aborted_heartbeat(self, abort: LeaseHeartbeatAbort) -> None: ...

    def recover_stale_owner(self, *, stale_after_seconds: float) -> object: ...


@dataclass(slots=True)
class StandbyLeaseControl:
    """State and safe operations exposed by the standby server."""

    lease: LeaseBackend
    database_url: str
    local_token: str | None
    promotion_requested: asyncio.Event
    schema_verifier: Callable[[str], None]

    async def status_payload(self) -> dict[str, object]:
        status = await asyncio.to_thread(self.lease.status)
        return {"mode": "standby", **asdict(status)}

    async def promote(self) -> tuple[bool, DaemonLeaseStatus]:
        await asyncio.to_thread(self.schema_verifier, self.database_url)
        acquired = await asyncio.to_thread(self.lease.try_acquire)
        if acquired:
            self.promotion_requested.set()
            return True, DaemonLeaseStatus(held=True)
        return False, await asyncio.to_thread(self.lease.status)

    async def recover(self, *, stale_after_seconds: float) -> None:
        await asyncio.to_thread(self.schema_verifier, self.database_url)
        await asyncio.to_thread(
            self.lease.recover_stale_owner,
            stale_after_seconds=stale_after_seconds,
        )
        acquired = await asyncio.to_thread(self.lease.try_acquire)
        if not acquired:
            raise DaemonLeaseError("lease was reacquired before recovery promotion")
        self.promotion_requested.set()


def create_standby_app(control: StandbyLeaseControl) -> FastAPI:
    """Create the deliberately tiny standby status/control application."""
    app = FastAPI(title="Gobby standby lease control", docs_url=None, redoc_url=None)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        token = control.local_token
        supplied = ""
        if authorization and authorization.startswith("Bearer "):
            supplied = authorization.removeprefix("Bearer ")
        if not token or not supplied or not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Authentication required")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "lease_mode": "standby"}

    @app.get("/api/admin/status")
    async def status() -> dict[str, object]:
        return {"status": "ok", "lease": await control.status_payload()}

    @app.get("/api/admin/lease/status")
    async def lease_status() -> dict[str, object]:
        return await control.status_payload()

    @app.post("/api/admin/lease/promote", dependencies=[])
    async def promote(authorization: str | None = Header(default=None)) -> dict[str, bool]:
        require_auth(authorization)
        acquired, owner = await control.promote()
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "active-daemon lease is held",
                    "owner_pid": owner.owner_pid,
                    "owner_application_name": owner.owner_application_name,
                    "heartbeat_age_seconds": owner.heartbeat_age_seconds,
                },
            )
        return {"promoting": True}

    @app.post("/api/admin/lease/recover", dependencies=[])
    async def recover(
        stale_after_seconds: float = 30.0,
        authorization: str | None = Header(default=None),
    ) -> dict[str, bool]:
        require_auth(authorization)
        try:
            await control.recover(stale_after_seconds=stale_after_seconds)
        except FreshLeaseOwnerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DaemonLeaseError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"recovered": True, "promoting": True}

    return app


async def serve_standby_until_promotion(
    control: StandbyLeaseControl,
    *,
    host: str,
    port: int,
) -> bool:
    """Serve lease control until promoted or externally stopped."""
    server = uvicorn.Server(
        uvicorn.Config(
            create_standby_app(control),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    server_task = asyncio.create_task(server.serve(), name="standby-lease-server")
    promotion_task = asyncio.create_task(
        control.promotion_requested.wait(),
        name="standby-promotion-wait",
    )
    done, _pending = await asyncio.wait(
        {server_task, promotion_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    promoted = promotion_task in done and promotion_task.result()
    if promoted:
        server.should_exit = True
    if not server_task.done():
        await server_task
    if not promotion_task.done():
        promotion_task.cancel()
        await asyncio.gather(promotion_task, return_exceptions=True)
    return bool(promoted)


LeaseLossReason = Literal["lease_heartbeat_timeout", "lease_connection_lost"]


@dataclass(frozen=True)
class LeaseLoss:
    """Typed lease-loss reason and bounded heartbeat cleanup diagnostics."""

    reason: LeaseLossReason
    heartbeat_elapsed_seconds: float
    heartbeat_timeout_seconds: float | None = None
    cancellation_outcome: str | None = None
    cancellation_error: str | None = None
    worker_settled: bool = True
    worker_settlement_timeout_seconds: float | None = None
    worker_settlement_elapsed_seconds: float | None = None

    def shutdown_details(self) -> dict[str, object]:
        details = asdict(self)
        details.pop("reason")
        return {key: value for key, value in details.items() if value is not None}


class _DaemonHeartbeatWorker:
    """Run one heartbeat on a daemon thread that cannot hold process exit."""

    def __init__(self, lease: LeaseBackend) -> None:
        self._lease = lease
        self._future: Future[None] = Future()
        self._lock = threading.Lock()
        self._abort: LeaseHeartbeatAbort | None = None
        self._settled = False
        self._thread = threading.Thread(
            target=self._run,
            name="gobby-lease-heartbeat",
            daemon=True,
        )

    @property
    def future(self) -> Future[None]:
        return self._future

    def start(self) -> None:
        self._thread.start()

    def abort(
        self,
        *,
        on_invalidated: Callable[[], None],
        cancel_timeout_seconds: float,
    ) -> LeaseHeartbeatAbort:
        abort = self._lease.abort_heartbeat(
            on_invalidated=on_invalidated,
            cancel_timeout_seconds=cancel_timeout_seconds,
        )
        close_now = False
        with self._lock:
            if self._settled:
                close_now = True
            else:
                self._abort = abort
        if close_now:
            self._lease.close_aborted_heartbeat(abort)
        return abort

    def _run(self) -> None:
        error: BaseException | None = None
        try:
            self._lease.heartbeat()
        except BaseException as exc:
            error = exc

        with self._lock:
            self._settled = True
            abort = self._abort
        if abort is not None:
            self._lease.close_aborted_heartbeat(abort)

        if error is None:
            self._future.set_result(None)
        else:
            self._future.set_exception(error)


_LEASE_HEARTBEAT_TIMEOUT_SECONDS = 15.0
_LEASE_HEARTBEAT_CANCEL_TIMEOUT_SECONDS = 1.0
_LEASE_HEARTBEAT_CLEANUP_TIMEOUT_SECONDS = 2.0


async def monitor_active_lease(
    lease: LeaseBackend,
    *,
    stop: asyncio.Event,
    on_loss: Callable[[LeaseLoss], None],
    on_invalidation: Callable[[], None] | None = None,
    heartbeat_interval_seconds: float = 2.0,
    heartbeat_timeout_seconds: float = _LEASE_HEARTBEAT_TIMEOUT_SECONDS,
    heartbeat_cancel_timeout_seconds: float = _LEASE_HEARTBEAT_CANCEL_TIMEOUT_SECONDS,
    heartbeat_cleanup_timeout_seconds: float = _LEASE_HEARTBEAT_CLEANUP_TIMEOUT_SECONDS,
) -> None:
    """Heartbeat the lease and request shutdown immediately after connection loss."""
    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat_interval_seconds must be positive")
    if heartbeat_timeout_seconds <= 0:
        raise ValueError("heartbeat_timeout_seconds must be positive")
    if heartbeat_cancel_timeout_seconds <= 0:
        raise ValueError("heartbeat_cancel_timeout_seconds must be positive")
    if heartbeat_cleanup_timeout_seconds <= 0:
        raise ValueError("heartbeat_cleanup_timeout_seconds must be positive")

    def invalidate() -> None:
        if on_invalidation is None:
            return
        try:
            on_invalidation()
        except Exception:
            logger.exception("Active-daemon lease invalidation callback failed")

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=heartbeat_interval_seconds)
            return
        except TimeoutError:
            pass
        loop = asyncio.get_running_loop()
        heartbeat_started = loop.time()
        worker = _DaemonHeartbeatWorker(lease)
        # Thread.start blocks on a condition until the new thread reports that
        # it is running, which is a blocking wait on the loop (#20845).
        await asyncio.to_thread(worker.start)
        heartbeat = asyncio.wrap_future(worker.future)

        def consume_heartbeat_result(future: asyncio.Future[None]) -> None:
            if not future.cancelled():
                future.exception()

        heartbeat.add_done_callback(consume_heartbeat_result)
        try:
            await asyncio.wait_for(
                asyncio.shield(heartbeat),
                timeout=heartbeat_timeout_seconds,
            )
        except TimeoutError:
            cleanup_started = loop.time()
            abort = worker.abort(
                on_invalidated=invalidate,
                cancel_timeout_seconds=heartbeat_cancel_timeout_seconds,
            )
            cleanup_elapsed = loop.time() - cleanup_started
            settlement_timeout = max(0.0, heartbeat_cleanup_timeout_seconds - cleanup_elapsed)
            worker_settled = heartbeat.done()
            if not worker_settled and settlement_timeout > 0:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(heartbeat),
                        timeout=settlement_timeout,
                    )
                except TimeoutError:
                    pass
                except LeaseConnectionLostError:
                    pass
                worker_settled = heartbeat.done()
            settlement_elapsed = loop.time() - cleanup_started
            loss = LeaseLoss(
                reason="lease_heartbeat_timeout",
                heartbeat_elapsed_seconds=loop.time() - heartbeat_started,
                heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                cancellation_outcome=abort.cancellation_outcome,
                cancellation_error=abort.cancellation_error,
                worker_settled=worker_settled,
                worker_settlement_timeout_seconds=heartbeat_cleanup_timeout_seconds,
                worker_settlement_elapsed_seconds=settlement_elapsed,
            )
            logger.error(
                "Active-daemon lease heartbeat timed out; requesting shutdown",
                extra=loss.shutdown_details(),
            )
            on_loss(loss)
            return
        except LeaseConnectionLostError:
            invalidate()
            loss = LeaseLoss(
                reason="lease_connection_lost",
                heartbeat_elapsed_seconds=loop.time() - heartbeat_started,
            )
            logger.error("Active-daemon lease connection lost; requesting shutdown")
            on_loss(loss)
            return


__all__ = [
    "LeaseBackend",
    "StandbyLeaseControl",
    "create_standby_app",
    "monitor_active_lease",
    "serve_standby_until_promotion",
]
