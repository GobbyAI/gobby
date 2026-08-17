"""Issue and revoke a per-invocation maintenance launch envelope."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import uuid4

from gobby.runtime_grants.handshake import HandshakeService
from gobby.runtime_grants.launch import ManagedLaunch, materialize_managed_launch
from gobby.storage.managed_credentials import ManagedCredentialManager

logger = logging.getLogger(__name__)


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
    def open(self, project_id: str, *, timeout_seconds: float) -> Iterator[ManagedLaunch]:
        execution_id = uuid4()
        dest = Path(tempfile.mkdtemp(prefix="gobby-mnt-"))
        issued = False
        try:
            grant = self._handshake.issue_for_maintenance(
                machine_id=self._machine_id,
                project_id=project_id,
                execution_id=str(execution_id),
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
                        "failed to revoke maintenance credential %s",
                        execution_id,
                        exc_info=True,
                    )
            shutil.rmtree(dest, ignore_errors=True)

    @asynccontextmanager
    async def open_async(
        self, project_id: str, *, timeout_seconds: float
    ) -> AsyncIterator[ManagedLaunch]:
        cm = self.open(project_id, timeout_seconds=timeout_seconds)
        launch = await asyncio.to_thread(cm.__enter__)
        try:
            yield launch
        except BaseException as exc:
            await asyncio.to_thread(cm.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            await asyncio.to_thread(cm.__exit__, None, None, None)
