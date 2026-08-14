"""Minimal HTTP control plane for a standby Gobby daemon."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol

import uvicorn
from fastapi import FastAPI, Header, HTTPException

from gobby.daemon_lease import (
    DaemonLeaseError,
    DaemonLeaseStatus,
    FreshLeaseOwnerError,
    LeaseConnectionLostError,
)

logger = logging.getLogger(__name__)


class LeaseBackend(Protocol):
    """Lease operations used by the standby control plane."""

    def status(self) -> DaemonLeaseStatus: ...

    def try_acquire(self) -> bool: ...

    def heartbeat(self) -> None: ...

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

    @app.get("/api/admin/health")
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


async def monitor_active_lease(
    lease: LeaseBackend,
    *,
    stop: asyncio.Event,
    on_loss: Callable[[], None],
    heartbeat_interval_seconds: float = 2.0,
) -> None:
    """Heartbeat the lease and request shutdown immediately after connection loss."""
    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat_interval_seconds must be positive")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=heartbeat_interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            await asyncio.to_thread(lease.heartbeat)
        except LeaseConnectionLostError:
            logger.error("Active-daemon lease connection lost; requesting shutdown")
            # Drain off the event loop so in-flight handlers can finish.
            await asyncio.to_thread(on_loss)
            return


__all__ = [
    "LeaseBackend",
    "StandbyLeaseControl",
    "create_standby_app",
    "monitor_active_lease",
    "serve_standby_until_promotion",
]
