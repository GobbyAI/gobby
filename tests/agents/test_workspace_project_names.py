"""Registered project names, system projects included, anchor agent workspaces."""

from pathlib import Path

import pytest

from gobby.agents.workspace_bindings import bind_session_workspace
from gobby.agents.workspace_projects import validate_workspace_project_name
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID, SYSTEM_PROJECT_NAMES
from gobby.workflows.state_manager import SessionVariableManager
from tests.mcp_proxy.tools.test_apply_agent_definition import register_session

pytestmark = pytest.mark.unit

UNDERSCORE_SYSTEM_NAMES = sorted(name for name in SYSTEM_PROJECT_NAMES if name.startswith("_"))


def _resolution(name: str) -> str:
    """Return the accepted name, or the refusal text so a refusal fails as an assertion."""
    try:
        return validate_workspace_project_name(name)
    except ValueError as error:
        return f"refused: {error}"


@pytest.mark.parametrize("name", UNDERSCORE_SYSTEM_NAMES)
def test_system_project_names_with_a_leading_underscore_are_accepted(name: str) -> None:
    assert _resolution(name) == name


def test_project_less_session_binds_a_personal_project_workspace(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    session_id = register_session(temp_db, monkeypatch)
    SessionVariableManager(temp_db).merge_variables(session_id, {"_agent_type": "default"})
    row = temp_db.fetchone("SELECT project_id, seq_num FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    assert row["project_id"] == PERSONAL_PROJECT_ID
    project = temp_db.fetchone("SELECT name FROM projects WHERE id = %s", (PERSONAL_PROJECT_ID,))
    assert project is not None
    expected = (
        tmp_path
        / "home"
        / "agents_workspaces"
        / project["name"]
        / "default"
        / f"session-{row['seq_num']}"
    )

    try:
        bound: Path | str | None = bind_session_workspace(temp_db, session_id)
    except ValueError as error:
        bound = f"refused: {error}"

    assert bound == expected
    assert expected.is_dir()
