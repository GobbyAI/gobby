"""Agent and step tool enforcement for the rule engine.

Handles tool allow/block lists at the agent and step workflow levels,
MCP tool matching, and step workflow transition processing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.agents.run_completion import complete_and_notify_agent_run
from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.workflows.engine.enforcement_audit import EnforcementAuditMixin
from gobby.workflows.engine.enforcement_checks import EnforcementCheckMixin
from gobby.workflows.engine.enforcement_completion import EnforcementCompletionMixin
from gobby.workflows.engine.enforcement_handlers import EnforcementHandlerMixin
from gobby.workflows.reserved_variables import RESERVED_WORKFLOW_VARIABLES
from gobby.workflows.step_instances import AgentStepInstanceManager

__all__ = [
    "EnforcementMixin",
    "RESERVED_WORKFLOW_VARIABLES",
    "cleanup_agent_runtime_state",
    "complete_and_notify_agent_run",
]


class EnforcementMixin(
    EnforcementAuditMixin,
    EnforcementCheckMixin,
    EnforcementHandlerMixin,
    EnforcementCompletionMixin,
):
    """Mixin providing tool enforcement methods for RuleEngine."""

    db: HubDatabase
    instance_manager: AgentStepInstanceManager
    workflow_audit: WorkflowAuditManager

    if TYPE_CHECKING:
        from gobby.agents.runner import AgentRunner
        from gobby.events.completion_registry import CompletionEventRegistry

        # Provided by TemplatingMixin at runtime via RuleEngine MRO
        def _evaluate_condition(
            self, condition: str, ctx: dict[str, Any], effect_type: str
        ) -> bool: ...

        _runner: AgentRunner | None
        _completion_registry: CompletionEventRegistry | None
