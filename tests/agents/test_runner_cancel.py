"""Agent cancellation preserves terminal child sessions in the real session store."""

from unittest.mock import patch

import pytest

from gobby.agents.runner import AgentRunner
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory


@pytest.mark.integration
@pytest.mark.parametrize("terminal_status", ["expired", "deleted"])
def test_cancel_run_preserves_terminal_child(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    terminal_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000002"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    project = isolated_checkout_factory(temp_db, "cancel-terminal-child").project
    sessions = SessionManager(temp_db)
    child = sessions.register(
        external_id="cancel-terminal-child",
        machine_id=machine_id,
        source="codex",
        project_id=project.id,
    )
    sessions.update_status(child.id, terminal_status)
    runner = AgentRunner(db=temp_db, session_storage=sessions)
    with (
        patch.object(runner._run_storage, "get") as get_run,
        patch.object(runner._run_storage, "cancel") as cancel_run,
    ):
        get_run.return_value.status = "running"
        get_run.return_value.child_session_id = child.id

        assert runner.cancel_run("terminal-child-run") is True

        cancel_run.assert_called_once_with("terminal-child-run")
    preserved = sessions.get(child.id)
    assert preserved is not None
    assert preserved.status == terminal_status
