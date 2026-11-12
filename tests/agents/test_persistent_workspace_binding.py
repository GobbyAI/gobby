"""Stable ownership and recoverable rotation for synthetic persistent definitions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from gobby.agents import persistent_workspaces
from gobby.agents.agent_workspaces import workspace_anchor
from gobby.agents.workspace_bindings import bind_session_workspace
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from tests.agents.test_agent_workspace_lifecycle import _define
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.hooks.test_clear_successor_seat import _pane

pytestmark = pytest.mark.unit


def test_concurrent_activation_admits_one_owner(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "concurrent")
    _define(temp_db, "fixture-persistent", persistent=True)
    second = pane.register("second-ext")
    variables = SessionVariableManager(temp_db)
    for session_id in (pane.predecessor_id, second):
        variables.merge_variables(session_id, {"_agent_type": "fixture-persistent"})
    barrier = Barrier(2, timeout=10)

    def activate(session_id: str) -> Path | str | None:
        barrier.wait()
        try:
            return bind_session_workspace(temp_db, session_id)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(activate, session_id) for session_id in (pane.predecessor_id, second)
        ]
        results = [future.result(timeout=30) for future in futures]
    assert sum(isinstance(result, Path) for result in results) == 1
    assert sum(isinstance(result, str) and "live owner" in result for result in results) == 1


def test_same_definition_owners_are_isolated_by_project(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    first = _pane(temp_db, tmp_path, monkeypatch, "project-one")
    project = install_isolated_checkout_project(
        temp_db,
        tmp_path / "project-two",
        name="project-two",
        machine_id=first.machine_id,
        monkeypatch=monkeypatch,
    )
    second = replace(first, project_id=project.project.id, root=project.root_path)
    second.predecessor_id = second.register("project-two-ext")
    _define(temp_db, "fixture-persistent", persistent=True)
    variables = SessionVariableManager(temp_db)
    for pane in (first, second):
        variables.merge_variables(pane.predecessor_id, {"_agent_type": "fixture-persistent"})
    one = bind_session_workspace(temp_db, first.predecessor_id)
    two = bind_session_workspace(temp_db, second.predecessor_id)
    assert one is not None and two is not None and one != two
    (one / "notes.md").write_text("project one")
    assert not (two / "notes.md").exists()
    assert not (one.parent / "history").exists()
    assert not (two.parent / "history").exists()


def test_persistent_owner_resumes_in_one_stable_folder(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "persistent")
    _define(temp_db, "fixture-persistent", persistent=True)
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(pane.predecessor_id, {"_agent_type": "fixture-persistent"})
    expected = workspace_anchor(pane.project_id) / "fixture-persistent" / "current"
    assert bind_session_workspace(temp_db, pane.predecessor_id) == expected
    (expected / "notes.md").write_text("current owner")
    assert bind_session_workspace(temp_db, pane.predecessor_id) == expected
    assert (expected / "notes.md").read_text() == "current owner"
    assert not (expected.parent / "history").exists()


def test_new_owner_refuses_live_holder_then_rotates_without_losing_history(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "takeover")
    _define(temp_db, "fixture-persistent", persistent=True)
    variables = SessionVariableManager(temp_db)
    first = pane.predecessor_id
    second = pane.register("second-ext")
    for session_id in (first, second):
        variables.merge_variables(session_id, {"_agent_type": "fixture-persistent"})
    folder = bind_session_workspace(temp_db, first)
    assert folder is not None
    (folder / "notes.md").write_text("first owner")
    with pytest.raises(ValueError, match="live owner"):
        bind_session_workspace(temp_db, second)
    assert (folder / "notes.md").read_text() == "first owner"
    pane.sessions.update_status_if_non_terminal(first, "expired")
    assert bind_session_workspace(temp_db, second) == folder
    first_row = pane.sessions.get(first)
    assert first_row is not None
    archive = folder.parent / "history" / f"session-{first_row.seq_num}"
    assert (archive / "notes.md").read_text() == "first owner"
    assert not (folder / "notes.md").exists()
    (folder / "notes.md").write_text("second owner")
    assert bind_session_workspace(temp_db, second) == folder
    assert (archive / "notes.md").read_text() == "first owner"
    assert (folder / "notes.md").read_text() == "second owner"


def test_interrupted_rotation_replays_without_a_second_archive(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "interrupted")
    _define(temp_db, "fixture-persistent", persistent=True)
    variables = SessionVariableManager(temp_db)
    first, second = pane.predecessor_id, pane.register("second-ext")
    variables.merge_variables(first, {"_agent_type": "fixture-persistent"})
    folder = bind_session_workspace(temp_db, first)
    assert folder is not None
    (folder / "notes.md").write_text("recover me")
    pane.sessions.update_status_if_non_terminal(first, "expired")
    variables.merge_variables(second, {"_agent_type": "fixture-persistent"})
    save = persistent_workspaces._save

    def interrupt(path: Path, state: dict[str, object]) -> None:
        if state.get("owner") == second and "pending" not in state:
            raise OSError("simulated process interruption")
        save(path, state)

    with monkeypatch.context() as interruption:
        interruption.setattr(persistent_workspaces, "_save", interrupt)
        with pytest.raises(OSError, match="simulated process interruption"):
            bind_session_workspace(temp_db, second)
    assert bind_session_workspace(temp_db, second) == folder
    archives = list((folder.parent / "history").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "notes.md").read_text() == "recover me"
    assert not (folder / "notes.md").exists()


def test_archive_collision_keeps_both_copies_untouched(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "collision")
    _define(temp_db, "fixture-persistent", persistent=True)
    variables = SessionVariableManager(temp_db)
    first, second = pane.predecessor_id, pane.register("second-ext")
    variables.merge_variables(first, {"_agent_type": "fixture-persistent"})
    folder = bind_session_workspace(temp_db, first)
    assert folder is not None
    (folder / "notes.md").write_text("live copy")
    row = pane.sessions.get(first)
    assert row is not None
    archive = folder.parent / "history" / f"session-{row.seq_num}"
    archive.mkdir(parents=True)
    (archive / "notes.md").write_text("original archive")
    pane.sessions.update_status_if_non_terminal(first, "expired")
    variables.merge_variables(second, {"_agent_type": "fixture-persistent"})
    with pytest.raises(ValueError, match="collision"):
        bind_session_workspace(temp_db, second)
    assert (folder / "notes.md").read_text() == "live copy"
    assert (archive / "notes.md").read_text() == "original archive"


def test_legacy_sessions_remain_separate_in_history(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "migration")
    _define(temp_db, "fixture-persistent", persistent=True)
    root = workspace_anchor(pane.project_id) / "fixture-persistent"
    for sequence in (101, 102):
        legacy = root / f"session-{sequence}"
        legacy.mkdir(parents=True)
        (legacy / "same-name.md").write_text(str(sequence))
    SessionVariableManager(temp_db).merge_variables(
        pane.predecessor_id, {"_agent_type": "fixture-persistent"}
    )
    assert bind_session_workspace(temp_db, pane.predecessor_id) == root / "current"
    for sequence in (101, 102):
        assert not (root / f"session-{sequence}").exists()
        assert (root / "history" / f"session-{sequence}" / "same-name.md").read_text() == str(
            sequence
        )


def test_default_cleanup_removes_only_empty_generated_folders(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "default-cleanup")
    root = workspace_anchor(pane.project_id) / "default"
    empty = root / "session-101"
    historical = root / "session-102"
    empty.mkdir(parents=True)
    historical.mkdir()
    (historical / "empty-recovery-directory").mkdir()
    (root / "unrelated-empty-directory").mkdir()
    (historical / "notes.md").write_text("retain historical data")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        pane.predecessor_id,
        {"_agent_type": "default", "_agent_workspace_dir": str(historical)},
    )
    assert bind_session_workspace(temp_db, pane.predecessor_id) is None
    assert not empty.exists()
    assert (historical / "notes.md").read_text() == "retain historical data"
    assert (historical / "empty-recovery-directory").is_dir()
    assert (root / "unrelated-empty-directory").is_dir()
    assert variables.get_variables(pane.predecessor_id)["_agent_workspace_dir"] is None


def test_default_cleanup_clears_flags_without_a_folder_binding(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.agents.workspace_bindings import cleanup_default_workspaces

    pane = _pane(temp_db, tmp_path, monkeypatch, "default-flags")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        pane.predecessor_id,
        {
            "_agent_type": "default",
            "_agent_workspace_persistent": True,
            "_agent_workspace_injected": True,
        },
    )
    with temp_db.transaction():
        cleanup_default_workspaces(temp_db, pane.project_id)
    state = variables.get_variables(pane.predecessor_id)
    assert state["_agent_workspace_persistent"] is False
    assert state["_agent_workspace_injected"] is False


def test_default_activation_clears_multiple_stale_sessions_atomically(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "default-batch")
    variables = SessionVariableManager(temp_db)
    stale_ids = [
        pane.sessions.register_session(
            external_id=f"stale-{index}",
            machine_id=pane.machine_id,
            source="grok",
            project_id=pane.project_id,
        )
        for index in range(2)
    ]
    for session_id in stale_ids:
        variables.merge_variables(
            session_id,
            {"_agent_type": "default", "_agent_workspace_injected": True},
        )

    assert bind_session_workspace(temp_db, pane.predecessor_id) is None
    for session_id in stale_ids:
        assert variables.get_variables(session_id)["_agent_workspace_injected"] is False
    assert variables.get_variables(pane.predecessor_id)["_agent_workspace_dir"] is None


def test_default_cleanup_tolerates_a_concurrently_removed_folder(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.agents.workspace_bindings import cleanup_default_workspaces

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "default-concurrent")
    root = workspace_anchor(pane.project_id) / "default"
    empty = root / "session-101"
    empty.mkdir(parents=True)
    remove = Path.rmdir

    def remove_after_other_cleanup(folder: Path) -> None:
        if folder == empty:
            remove(folder)
        remove(folder)

    monkeypatch.setattr(Path, "rmdir", remove_after_other_cleanup)
    cleanup_default_workspaces(temp_db, pane.project_id)
    assert not root.exists()


@pytest.mark.parametrize("archive", ["../escaped", "session--1", "session-x"])
def test_rotation_refuses_invalid_archive_paths(tmp_path: Path, archive: str) -> None:
    folder = tmp_path / "workspace"
    current = folder / "current"
    current.mkdir(parents=True)
    (current / "notes.md").write_text("keep")
    with pytest.raises(persistent_workspaces.WorkspaceOwnershipError, match="journal"):
        persistent_workspaces._finish_rotation(folder, {"archive": archive})
    assert (current / "notes.md").read_text() == "keep"


def test_launch_grant_acquires_ownership_before_session_start(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.agents.agent_workspaces import spawn_workspace_write_paths

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    pane = _pane(temp_db, tmp_path, monkeypatch, "launch-owner")
    _define(temp_db, "fixture-persistent", persistent=True)
    second = pane.register("second-ext")
    folders = spawn_workspace_write_paths(temp_db, pane.predecessor_id, "fixture-persistent")
    assert len(folders) == 1
    assert Path(folders[0]).name == "current"
    from gobby.agents.workspace_bindings import cleanup_default_workspaces

    cleanup_default_workspaces(temp_db, pane.project_id)
    assert bind_session_workspace(temp_db, pane.predecessor_id) == Path(folders[0])
    with pytest.raises(persistent_workspaces.WorkspaceOwnershipError, match="live owner"):
        spawn_workspace_write_paths(temp_db, second, "fixture-persistent")
