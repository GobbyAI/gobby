"""Lifecycle dispatcher contracts."""

from gobby.dispatch.actions import (
    Action,
    AppendAuditMarkerAction,
    CreateIsolationAction,
    EscalateAction,
    SpawnAgentAction,
    StartPipelineAction,
)

__all__ = [
    "Action",
    "AppendAuditMarkerAction",
    "CreateIsolationAction",
    "EscalateAction",
    "SpawnAgentAction",
    "StartPipelineAction",
]
