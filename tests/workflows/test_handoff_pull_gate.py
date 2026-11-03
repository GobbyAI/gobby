"""A resumed epoch consumes its durable handoff before ordinary tools."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.utils.session_context import SessionContext, reset_session_context, set_session_context
from gobby.workflows.safe_evaluator import build_condition_helpers

pytestmark = pytest.mark.unit
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("value", [None, True, {"attempt_id": "forged"}])
def test_pending_pull_marker_cannot_be_cleared_or_forged(value: Any) -> None:
    variables = MagicMock()
    sessions = MagicMock()
    sessions.resolve_session_reference.return_value = SESSION_ID
    token = set_session_context(SessionContext(session_id=SESSION_ID))
    try:
        with (
            patch(
                "gobby.mcp_proxy.tools.workflows._variables.resolve_session_id",
                return_value=SESSION_ID,
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager") as authority,
        ):
            authority.return_value.get_variable_subset.return_value = {"_agent_type": "default"}
            result = set_variable(
                sessions,
                MagicMock(),
                "handoff_pull_pending",
                value,
                SESSION_ID,
                session_var_manager=variables,
            )
    finally:
        reset_session_context(token)
    assert result["success"] is False
    assert "reserved variable 'handoff_pull_pending'" in result["error"]
    variables.set_variable.assert_not_called()


def test_retired_task_tree_helper_is_not_registered() -> None:
    assert "task_tree_complete" not in build_condition_helpers()
