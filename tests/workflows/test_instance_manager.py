"""Legacy WorkflowInstanceManager suite is gone; typed manager owns the seam."""

from __future__ import annotations

import gobby.workflows.definitions as definitions
import gobby.workflows.state_manager as state_manager
from gobby.workflows.step_instances import AgentStepInstanceManager


def test_legacy_instance_manager_symbols_are_gone() -> None:
    assert not hasattr(state_manager, "WorkflowInstanceManager")
    assert not hasattr(definitions, "WorkflowInstance")
    assert hasattr(AgentStepInstanceManager, "get_for_session")
    assert hasattr(AgentStepInstanceManager, "delete_for_session")
    assert not hasattr(AgentStepInstanceManager, "get_instance")
    assert not hasattr(AgentStepInstanceManager, "delete_instances_for_session")
