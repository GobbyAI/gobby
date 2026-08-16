"""Narrow per-project managed launch port for daemon gcode children."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from gobby.runtime_grants.launch import ManagedLaunch


class MaintenanceLaunchFactory(Protocol):
    def open(
        self, project_id: str, *, timeout_seconds: float
    ) -> AbstractContextManager[ManagedLaunch]: ...


@contextmanager
def unavailable_launch(project_id: str, *, timeout_seconds: float) -> Iterator[ManagedLaunch]:
    del project_id, timeout_seconds
    raise RuntimeError("maintenance launch factory is not configured")
    yield  # pragma: no cover
