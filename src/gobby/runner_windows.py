"""Windows retains the Python PID/lease lifecycle until native supervision exists."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.daemon_lease import ActiveDaemonLease
from gobby.daemon_lease_control import (
    LeaseLoss,
    StandbyLeaseControl,
    monitor_active_lease,
    serve_standby_until_promotion,
)
from gobby.runner_pid_file import (
    PidOwnershipResolution,
    adopt_inherited_claim,
    claim_pid_file,
    convert_or_acquire_service_claim,
)
from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent
from gobby.storage.schema_contract import verify_schema

if TYPE_CHECKING:
    from gobby.config.bootstrap import BootstrapConfig
    from gobby.runner import GobbyRunner


def claim_windows_pid(pid_file: Path) -> PidOwnershipResolution | None:
    try:
        if os.environ.get("GOBBY_SERVICE_LAUNCH") == "1":
            return convert_or_acquire_service_claim(pid_file)
        return adopt_inherited_claim(pid_file) or claim_pid_file(pid_file)
    finally:
        os.environ.pop("GOBBY_SERVICE_LAUNCH", None)
        os.environ.pop("GOBBY_SERVICE_NONCE", None)


async def acquire_windows_lease(lease: ActiveDaemonLease, bootstrap: BootstrapConfig) -> bool:
    if await asyncio.to_thread(lease.try_acquire):
        return True
    assert bootstrap.database_url is not None
    return await serve_standby_until_promotion(
        StandbyLeaseControl(
            lease=lease,
            database_url=bootstrap.database_url,
            front_door_secret=None,
            promotion_requested=asyncio.Event(),
            schema_verifier=verify_schema,
        ),
        host=bootstrap.bind_host,
        port=bootstrap.daemon_port,
    )


async def run_windows_backend(
    runner: GobbyRunner, lease: ActiveDaemonLease, ownership: PidOwnershipResolution
) -> None:
    from gobby.servers.lease_fence import drain_effect_fence

    def invalidate() -> None:
        drain_effect_fence(getattr(runner.http_server, "effect_fence", None))

    def lost(loss: LeaseLoss) -> None:
        write_shutdown_intent(loss.reason, ShutdownIntent.STOP, details=loss.shutdown_details())
        runner.request_shutdown(ShutdownIntent.STOP)

    stop = asyncio.Event()
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            monitor_active_lease(lease, stop=stop, on_loss=lost, on_invalidation=invalidate),
            name="active-daemon-lease-monitor",
        )
        try:
            await runner.run(ownership_resolution=ownership)
        finally:
            stop.set()
