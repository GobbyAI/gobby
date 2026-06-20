"""Linear sync service facade.

The implementation lives in focused helper modules. This module preserves the
existing import surface for CLI commands, daemon startup, and tests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.integrations.linear import LinearIntegration
from gobby.sync.linear_support import (
    LinearNotFoundError,
    LinearRateLimitError,
    LinearSyncError,
    _extract_record,
    _extract_records,
    _gobby_seq_from_linear_title,
    _linear_fetch_failure_limiter,
    _local_title_from_linear,
    _RepeatedFetchFailureLimiter,
)
from gobby.sync.linear_task_ops import LinearTaskOpsMixin

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

__all__ = [
    "LinearSyncService",
    "LinearSyncError",
    "LinearRateLimitError",
    "LinearNotFoundError",
    "create_linear_sync_handler",
]

# Keep private compatibility imports live for callers that patch through this facade.
_COMPAT_PRIVATE_EXPORTS = (
    _RepeatedFetchFailureLimiter,
    _extract_record,
    _extract_records,
    _gobby_seq_from_linear_title,
    _linear_fetch_failure_limiter,
    _local_title_from_linear,
)

logger = logging.getLogger(__name__)


class LinearSyncService(LinearTaskOpsMixin):
    """Service for syncing gobby tasks with Linear issues."""

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        task_manager: LocalTaskManager,
        project_id: str,
        linear_team_id: str | None = None,
        linear_project_id: str | None = None,
        project_manager: LocalProjectManager | None = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.task_manager = task_manager
        self.project_id = project_id
        self.linear_team_id = linear_team_id
        self.linear_project_id = linear_project_id
        self.linear = LinearIntegration(mcp_manager)
        self._project_manager = project_manager

    def is_available(self) -> bool:
        """Check if Linear MCP server is available."""
        return self.linear.is_available()


def create_linear_sync_handler(
    mcp_manager: MCPClientManager,
    task_manager: LocalTaskManager,
    project_id: str,
    team_id: str,
    linear_project_id: str | None = None,
) -> Any:
    """Create a cron handler for periodic Linear sync."""
    from gobby.storage.cron_models import CronJob

    async def linear_sync_handler(job: CronJob) -> str:
        service = LinearSyncService(
            mcp_manager=mcp_manager,
            task_manager=task_manager,
            project_id=project_id,
            linear_team_id=team_id,
            linear_project_id=linear_project_id,
        )

        if not service.is_available():
            return "Linear MCP server unavailable, skipping sync"

        try:
            result = await service.sync_all(team_id=team_id)
            pull = result["pull"]
            push = result["push"]
            pull_errors = int(pull.get("errors", 0))
            push_errors = int(push.get("errors", 0))
            if pull_errors or push_errors:
                raise RuntimeError(
                    "Linear sync completed with errors: "
                    f"pull_errors={pull_errors}, push_errors={push_errors}"
                )
            return (
                f"Linear sync complete: "
                f"pulled {pull['updated']} (skipped {pull['skipped']}, errors {pull_errors}), "
                f"pushed {push['pushed']} (errors {push_errors})"
            )
        except Exception as e:
            logger.error("Linear sync cron failed: %s", e, exc_info=True)
            raise

    return linear_sync_handler
