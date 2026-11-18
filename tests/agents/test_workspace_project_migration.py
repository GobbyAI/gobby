"""Whole-root migration preserves artifacts and refuses unsafe adoption."""

import json
import subprocess
from pathlib import Path

import pytest

from gobby.agents.agent_workspaces import workspaces_root
from gobby.agents.workspace_projects import (
    validate_workspace_project_name,
    workspace_project_name,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests.hooks.test_clear_successor_seat import _pane


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
    from gobby.agents.workspace_projects import registered_workspace_project_name
    from gobby.storage.projects import LocalProjectManager

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "workspace-name")
    name = registered_workspace_project_name(temp_db, pane.project_id)
    assert not workspaces_root().exists()
    LocalProjectManager(temp_db).create(name.upper())
    with pytest.raises(ValueError, match="Ambiguous"):
        registered_workspace_project_name(temp_db, pane.project_id)
    assert not workspaces_root().exists()


def test_migration_preserves_git_files_and_replays_interrupted_anchor_move(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from gobby.agents import workspace_backups

    caller = threading.current_thread()
    initialize = workspace_backups._init_git_dir

    def initialize_on_worker(project_name: str) -> None:
        assert threading.current_thread() is not caller
        initialize(project_name)

    monkeypatch.setattr(workspace_backups, "_init_git_dir", initialize_on_worker)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "workspace-migration")
    root = workspaces_root()
    old = root / pane.project_id
    old_git = root / f"{pane.project_id}.git"
    old.mkdir(parents=True)
    (old / "notes.md").write_text("keep this history\n")
    subprocess.run(["git", "init", "--bare", "--quiet", str(old_git)], check=True)
    subprocess.run(
        ["git", f"--git-dir={old_git}", f"--work-tree={old}", "add", "notes.md"], check=True
    )
    subprocess.run(
        [
            "git",
            f"--git-dir={old_git}",
            f"--work-tree={old}",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "preserved",
        ],
        check=True,
    )
    metadata = old_git / "workspace-owners" / "fixture-seat.json"
    metadata.parent.mkdir()
    owner_state = json.dumps(
        {"owner": pane.predecessor_id, "seq": 1, "pending": {"archive": "session-1"}}
    )
    metadata.write_text(owner_state)
    original_rename = Path.rename

    def interrupt_anchor(path: Path, target: Path) -> Path:
        if path == old:
            raise OSError("interrupted anchor move")
        return original_rename(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupt_anchor)
        with pytest.raises(OSError, match="interrupted anchor move"):
            workspace_project_name(temp_db, pane.project_id)
    name = workspace_project_name(temp_db, pane.project_id)
    target, gitdir = root / name, root / f"{name}.git"
    assert (target / "notes.md").read_text() == "keep this history\n"
    assert (gitdir / "workspace-owners/fixture-seat.json").read_text() == owner_state
    result = subprocess.run(
        ["git", f"--git-dir={gitdir}", "log", "-1", "--format=%s"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.strip() == "preserved"
    config = subprocess.run(
        ["git", f"--git-dir={gitdir}", "config", "core.worktree"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert config.stdout.strip() == str(target)
    assert workspace_project_name(temp_db, pane.project_id) == name
    assert not old.exists()
    assert not old_git.exists()


@pytest.mark.parametrize("obstruction", ["collision", "symlink", "live-owner"])
def test_migration_refuses_unsafe_destination_or_live_owner(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, obstruction: str
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "workspace-refusal")
    root = workspaces_root()
    old = root / pane.project_id
    old.mkdir(parents=True)
    note = old / "notes.md"
    note.write_text("preserve")
    project = temp_db.execute(
        "SELECT name FROM projects WHERE id = %s", (pane.project_id,)
    ).fetchone()
    assert project is not None
    target = root / project["name"]
    if obstruction == "collision":
        target.mkdir()
    elif obstruction == "symlink":
        target.symlink_to(old, target_is_directory=True)
    else:
        SessionVariableManager(temp_db).merge_variables(
            pane.predecessor_id, {"_agent_workspace_dir": str(old / "fixture-seat/current")}
        )
    with pytest.raises(ValueError, match="collision|symbolic links|live owner"):
        workspace_project_name(temp_db, pane.project_id)
    assert note.read_text() == "preserve"
