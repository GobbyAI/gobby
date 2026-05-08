"""Lifecycle dispatcher contracts."""

from gobby.dispatch.actions import (
    Action,
    AdvanceLifecycleAction,
    AppendAuditMarkerAction,
    CreateIsolationAction,
    EscalateAction,
    SpawnAgentAction,
    StartPipelineAction,
)

__all__ = [
    "Action",
    "AdvanceLifecycleAction",
    "AppendAuditMarkerAction",
    "CreateIsolationAction",
    "EscalateAction",
    "SpawnAgentAction",
    "StartPipelineAction",
]
