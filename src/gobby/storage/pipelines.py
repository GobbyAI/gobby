"""Local pipeline execution storage manager."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_executions import PipelineExecutionStorageMixin
from gobby.storage.pipeline_steps import PipelineStepStorageMixin
from gobby.storage.pipeline_subscribers import PipelineCompletionSubscriberMixin

__all__ = ["LocalPipelineExecutionManager"]


class LocalPipelineExecutionManager(
    PipelineExecutionStorageMixin,
    PipelineStepStorageMixin,
    PipelineCompletionSubscriberMixin,
):
    """Manager for local pipeline execution storage."""

    db: HubDatabase
    project_id: str | None

    def __init__(self, db: HubDatabase, project_id: str | None):
        """Initialize with database connection and project context.

        Args:
            db: Database connection
            project_id: Project ID for scoping executions
        """
        if project_id == "":
            raise ValueError("project_id is required")
        self.db = db
        self.project_id = project_id
