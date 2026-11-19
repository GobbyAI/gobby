"""Registered project names, system projects included, anchor agent workspaces."""

import json
from pathlib import Path

import pytest

from gobby.agents.agent_workspaces import workspaces_root
from gobby.agents.workspace_bindings import bind_session_workspace
from gobby.agents.workspace_projects import (
    registered_workspace_project_name,
    validate_workspace_project_name,
    workspace_project_name,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID, SYSTEM_PROJECT_NAMES, LocalProjectManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.hooks.test_clear_successor_seat import _pane
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


@pytest.mark.parametrize(
    "name",
    [
        "../other",
        "/absolute",
        "a/b",
        "a\\b",
        "",
        ".hidden",
        "a.git",
        "A.GIT",
        "11111111-1111-4111-8111-111111111111",
    ],
)
def test_unsafe_project_components_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid project name"):
        validate_workspace_project_name(name)


def test_metadata_resolution_refuses_case_collisions_without_creating_local_files(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "workspace-name")
    name = registered_workspace_project_name(temp_db, pane.project_id)
    assert not workspaces_root().exists()
    LocalProjectManager(temp_db).create(name.upper())
    with pytest.raises(ValueError, match="Ambiguous"):
        registered_workspace_project_name(temp_db, pane.project_id)
    assert not workspaces_root().exists()


@pytest.mark.parametrize(
    ("obstruction", "refusal"),
    [
        ("folder-without-receipt", "has no ownership receipt"),
        ("symlink", "symbolic links"),
        ("receipt-of-another-project", "belongs to another project"),
    ],
)
def test_resolution_refuses_a_destination_it_does_not_own(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    obstruction: str,
    refusal: str,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "workspace-refusal")
    root = workspaces_root()
    target = root / registered_workspace_project_name(temp_db, pane.project_id)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    note = elsewhere / "notes.md"
    note.write_text("preserve")
    root.mkdir(parents=True)
    if obstruction == "folder-without-receipt":
        target.mkdir()
    elif obstruction == "symlink":
        target.symlink_to(elsewhere, target_is_directory=True)
    else:
        marker = root / f"{target.name}.git" / "workspace-project.json"
        marker.parent.mkdir()
        marker.write_text(json.dumps({"project_id": "another-project", "name": target.name}))
    with pytest.raises(ValueError, match=refusal):
        workspace_project_name(temp_db, pane.project_id)
    assert note.read_text() == "preserve"


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
