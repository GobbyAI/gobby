"""Dispatcher spawn exception types."""

from __future__ import annotations

from collections.abc import Sequence


class DispatchSpawnUnavailable(RuntimeError):
    """Raised when dispatcher lacks the daemon services needed to spawn."""


class DispatchSpawnFailed(RuntimeError):
    """Raised when daemon services decline or fail an attempted spawn."""

    def __init__(
        self,
        message: str,
        *,
        stage_failure_cited_subtasks: Sequence[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage_failure_cited_subtasks: tuple[str, ...] = tuple(
            stage_failure_cited_subtasks or ()
        )
