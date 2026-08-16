"""Issue and revoke a per-invocation maintenance launch envelope."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from gobby.runtime_grants.handshake import HandshakeService
from gobby.runtime_grants.launch import ManagedLaunch, materialize_managed_launch
from gobby.storage.managed_credentials import ManagedCredentialManager


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
        try:
            grant = self._handshake.issue_for_maintenance(
                machine_id=self._machine_id,
                project_id=project_id,
                execution_id=str(execution_id),
            )
            launch = materialize_managed_launch(
                grant,
                dest_dir=dest,
                operator_token=self._operator_token,
                deadline_seconds=timeout_seconds,
            )
            yield launch
        finally:
            try:
                self._credentials.revoke(execution_id, reason="maintenance-complete")
            finally:
                shutil.rmtree(dest, ignore_errors=True)
