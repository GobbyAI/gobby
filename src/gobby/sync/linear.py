"""Linear sync service facade.

The implementation lives in focused helper modules. This module preserves the
existing import surface for CLI commands, daemon startup, and tests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.integrations.linear import LinearIntegration
from gobby.integrations.linear_graphql import LinearGraphQLClient
from gobby.storage.secrets import SecretDecryptionError
from gobby.sync.linear_support import (
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
        self.linear = LinearIntegration(mcp_manager, project_id=project_id)
        self._project_manager = project_manager

    def _graphql_availability(self) -> tuple[bool, str | None]:
        """Return whether stored Linear GraphQL credentials are usable."""
        try:
            client = LinearGraphQLClient.from_database(self.task_manager.db)
        except SecretDecryptionError:
            return False, "Linear GraphQL API key is configured but cannot be decrypted."

        if client is None:
            return (
                False,
                "Linear GraphQL API key is not configured. "
                "Set it with `gobby secrets set linear_api_key`.",
            )
        return True, None

    def is_available(self) -> bool:
        """Check whether either Linear MCP or GraphQL access is available."""
        if self.linear.is_available():
            return True
        graphql_available, _ = self._graphql_availability()
        return graphql_available

    def get_unavailable_reason(self) -> str | None:
        """Explain why neither Linear integration path is available."""
        if self.linear.is_available():
            return None

        graphql_available, graphql_reason = self._graphql_availability()
        if graphql_available:
            return None

        mcp_reason = self.linear.get_unavailable_reason()
        return " ".join(reason for reason in (mcp_reason, graphql_reason) if reason)


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
            reason = service.get_unavailable_reason() or "Linear integration unavailable"
            return f"{reason} Skipping sync."

        try:
            result = await service.sync_all(team_id=team_id)
            pull = result["pull"]
            push = result["push"]
            pull_errors = int(pull.get("errors", 0))
            push_errors = int(push.get("errors", 0))
            pull_deferred = int(pull.get("deferred", 0))
            push_deferred = int(push.get("deferred", 0))
            if pull_errors or push_errors:
                raise RuntimeError(
                    "Linear sync completed with errors: "
                    f"pull_errors={pull_errors}, push_errors={push_errors}, "
                    f"pull_deferred={pull_deferred}, push_deferred={push_deferred}"
                )
            status = "deferred" if pull_deferred or push_deferred else "complete"
            return (
                f"Linear sync {status}: "
                f"pulled {pull['updated']} "
                f"(skipped {pull['skipped']}, errors {pull_errors}, deferred {pull_deferred}), "
                f"pushed {push['pushed']} (errors {push_errors}, deferred {push_deferred})"
            )
        except Exception as e:
            logger.exception("Linear sync cron failed: %s", e)
            raise

    return linear_sync_handler
