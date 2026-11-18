"""Default scratch space uses an independent, readable session workspace."""

from pathlib import Path

import pytest

from gobby.agents.workspace_bindings import bind_session_workspace
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.hooks.test_clear_successor_seat import _pane

pytestmark = pytest.mark.unit


def test_default_binds_readable_session_workspace(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "default-scratch")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(pane.predecessor_id, {"_agent_type": "default"})
    project = LocalProjectManager(temp_db).get(pane.project_id)
    assert project is not None
    row = temp_db.fetchone("SELECT seq_num FROM sessions WHERE id = %s", (pane.predecessor_id,))
    assert row is not None
    expected = (
        tmp_path
        / "home"
        / "agents_workspaces"
        / project.name
        / "default"
        / f"session-{row['seq_num']}"
    )

    assert bind_session_workspace(temp_db, pane.predecessor_id) == expected
    assert expected.is_dir()
    note = expected / "notes.md"
    note.write_text("preserve this session")
    assert bind_session_workspace(temp_db, pane.predecessor_id) == expected
    assert note.read_text() == "preserve this session"
    assert variables.get_variable_subset(pane.predecessor_id, ["_agent_workspace_persistent"]) == {
        "_agent_workspace_persistent": False
    }

    successor = pane.register("fresh-session-ext")
    fresh = bind_session_workspace(temp_db, successor)
    assert fresh is not None and fresh != expected
    assert fresh.parent.name == "default"
    assert not (fresh / "notes.md").exists()
    assert note.read_text() == "preserve this session"
