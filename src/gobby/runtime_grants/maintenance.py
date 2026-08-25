"""Issue and revoke a per-invocation maintenance launch envelope."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

from gobby.runtime_grants.handshake import HandshakeService
from gobby.runtime_grants.launch import ManagedLaunch, materialize_managed_launch
from gobby.storage.managed_credentials import ManagedCredentialManager

logger = logging.getLogger(__name__)


async def _await_tracked[T](task: asyncio.Task[T]) -> tuple[T, asyncio.CancelledError | None]:
    """Finish a tracked task across repeated caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = exc


async def _await_exit(
    cm: Any,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> object:
    task = asyncio.create_task(asyncio.to_thread(cm.__exit__, exc_type, exc, tb))
    result, pending = await _await_tracked(task)
    if pending is not None:
        raise pending
    return result


class HandshakeMaintenanceLaunchFactory:
    """Create a fresh maintenance grant child environment for one gcode run."""

    def __init__(
        self,
        *,
        handshake: HandshakeService,
        credentials: ManagedCredentialManager,
        operator_token: str,
        machine_id: str,
    ) -> None:
        self._handshake = handshake
        self._credentials = credentials
        self._operator_token = operator_token
        self._machine_id = machine_id

    @contextmanager
    def open(
        self,
        project_id: str,
        *,
        timeout_seconds: float,
        code_overlay_project_id: str | None = None,
    ) -> Iterator[ManagedLaunch]:
        execution_id = uuid4()
        dest = Path(tempfile.mkdtemp(prefix="gobby-mnt-"))
        issued = False
        try:
            grant = self._handshake.issue_for_maintenance(
                machine_id=self._machine_id,
                project_id=project_id,
                execution_id=str(execution_id),
                code_overlay_project_id=code_overlay_project_id,
            )
            issued = True
            launch = materialize_managed_launch(
                grant,
                dest_dir=dest,
                operator_token=self._operator_token,
                deadline_seconds=timeout_seconds,
            )
            yield launch
        finally:
            if issued:
                try:
                    self._credentials.revoke(execution_id, reason="maintenance-complete")
                except Exception:
                    logger.warning(
                        "failed to revoke maintenance credential",
                        extra={"execution_id": str(execution_id)},
                        exc_info=True,
                    )
            shutil.rmtree(dest, ignore_errors=True)

    @asynccontextmanager
    async def open_async(
        self,
        project_id: str,
        *,
        timeout_seconds: float,
        code_overlay_project_id: str | None = None,
    ) -> AsyncIterator[ManagedLaunch]:
        cm = self.open(
            project_id,
            timeout_seconds=timeout_seconds,
            code_overlay_project_id=code_overlay_project_id,
        )
        enter_task = asyncio.create_task(asyncio.to_thread(cm.__enter__))
        launch, pending_cancel = await _await_tracked(enter_task)
        if pending_cancel is not None:
            await _await_exit(
                cm, type(pending_cancel), pending_cancel, pending_cancel.__traceback__
            )
            raise pending_cancel
        try:
            yield launch
        except BaseException as exc:
            await _await_exit(cm, type(exc), exc, exc.__traceback__)
            raise
        else:
            await _await_exit(cm, None, None, None)
