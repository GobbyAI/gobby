"""Project repo path protection for managed isolated agent sessions."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from gobby.hooks.project_context import ProjectIdResolver
from gobby.hooks.session_types import HookSessionManager
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    LocalProjectCheckoutManager,
    OverlayRegistrationRejectedError,
)
from gobby.storage.projects import IsolatedAgentProjectPathError, LocalProjectManager, Project
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.checkout_root import InvalidCheckoutRootError
from gobby.utils.project_context import ensure_project_json_for_isolation
from gobby.utils.project_init import initialize_project
from tests.fixtures.isolated_checkout import insert_isolated_machine, write_project_marker

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _isolation_path_variant(tmp_path: Path, isolated_path: Path, variant: str) -> str:
    if variant == "exact":
        return str(isolated_path)
    if variant == "trailing-separator":
        return f"{isolated_path}{os.sep}"

    alias_path = tmp_path / f"{isolated_path.name}-alias"
    alias_path.symlink_to(isolated_path, target_is_directory=True)
    return str(alias_path)


def _register_isolated_agent(
    db: HubDatabase,
    *,
    project: Project,
    isolated_path: Path,
    isolation: str,
) -> str:
    sessions = SessionManager(db)
    parent = sessions.register(
        external_id=f"parent-{isolation}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="test",
        project_id=project.id,
    )
    child = sessions.register(
        external_id=f"child-{isolation}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=project.id,
        parent_session_id=parent.id,
    )
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work in isolation",
    )

    if isolation == "worktree":
        worktree = LocalWorktreeManager(db).create(
            project_id=project.id,
            branch_name="task-worktree",
            worktree_path=str(isolated_path),
            agent_session_id=child.id,
        )
        runs.update_runtime(run.id, worktree_id=worktree.id)
    else:
        clone = LocalCloneManager(db).create(
            project_id=project.id,
            branch_name="task-clone",
            clone_path=str(isolated_path),
            agent_session_id=child.id,
        )
        runs.update_runtime(run.id, clone_id=clone.id)

    return child.id


def _seed_canonical_checkout(
    db: HubDatabase,
    canonical_path: Path,
    *,
    name: str,
) -> Project:
    insert_isolated_machine(db, LOCAL_MACHINE_ID)
    projects = LocalProjectManager(db)
    project = projects.create(name)
    write_project_marker(canonical_path, project_id=project.id, name=name)
    LocalProjectCheckoutManager(db).register(LOCAL_MACHINE_ID, project.id, str(canonical_path))
    return project


def _assert_canonical_checkout(db: HubDatabase, project_id: str, canonical_path: Path) -> None:
    project = LocalProjectManager(db).get(project_id)
    assert project is not None
    checkout = LocalProjectCheckoutManager(db).get(LOCAL_MACHINE_ID, project_id)
    assert checkout is not None
    assert checkout.root_path == str(canonical_path)


@pytest.mark.parametrize("isolation", ["worktree", "clone"])
def test_isolated_agent_init_preserves_primary_checkout(
    temp_db: HubDatabase,
    tmp_path: Path,
    isolation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_path = tmp_path / "canonical"
    isolated_path = tmp_path / isolation
    canonical_path.mkdir()
    isolated_path.mkdir()
    project = _seed_canonical_checkout(temp_db, canonical_path, name="shared-project")
    ensure_project_json_for_isolation(canonical_path, isolated_path)
    child_session_id = _register_isolated_agent(
        temp_db,
        project=project,
        isolated_path=isolated_path,
        isolation=isolation,
    )

    monkeypatch.setenv("GOBBY_SESSION_ID", child_session_id)
    with pytest.raises(IsolatedAgentProjectPathError):
        initialize_project(isolated_path, db=temp_db)

    _assert_canonical_checkout(temp_db, project.id, canonical_path)


@pytest.mark.parametrize("isolation", ["worktree", "clone"])
@pytest.mark.parametrize("variant", ["exact", "trailing-separator", "symlink"])
def test_hook_project_sync_preserves_primary_checkout_before_session_resolution(
    temp_db: HubDatabase,
    tmp_path: Path,
    isolation: str,
    variant: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_path = tmp_path / "canonical"
    isolated_path = tmp_path / isolation
    canonical_path.mkdir()
    isolated_path.mkdir()
    project = _seed_canonical_checkout(temp_db, canonical_path, name="shared-project")
    sessions = SessionManager(temp_db)
    _register_isolated_agent(
        temp_db,
        project=project,
        isolated_path=isolated_path,
        isolation=isolation,
    )
    project_path = _isolation_path_variant(tmp_path, isolated_path, variant)
    monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)

    ProjectIdResolver(session_manager=cast(HookSessionManager, sessions)).ensure_project_in_db(
        {
            "id": project.id,
            "name": project.name,
            "project_path": project_path,
        }
    )

    _assert_canonical_checkout(temp_db, project.id, canonical_path)


@pytest.mark.parametrize("isolation", ["worktree", "clone"])
@pytest.mark.parametrize("variant", ["exact", "trailing-separator", "symlink"])
def test_registered_overlay_cannot_replace_primary_checkout(
    temp_db: HubDatabase,
    tmp_path: Path,
    isolation: str,
    variant: str,
) -> None:
    canonical_path = tmp_path / "canonical"
    isolated_path = tmp_path / isolation
    canonical_path.mkdir()
    isolated_path.mkdir()
    project = _seed_canonical_checkout(temp_db, canonical_path, name="shared-project")
    _register_isolated_agent(
        temp_db,
        project=project,
        isolated_path=isolated_path,
        isolation=isolation,
    )
    overlay_path = _isolation_path_variant(tmp_path, isolated_path, variant)

    with pytest.raises(
        (
            IsolatedAgentProjectPathError,
            OverlayRegistrationRejectedError,
            InvalidCheckoutRootError,
        ),
        match="isolated agent session|registered overlay|normalized absolute path",
    ):
        LocalProjectManager(temp_db).update(project.id, repo_path=overlay_path)

    _assert_canonical_checkout(temp_db, project.id, canonical_path)


@pytest.mark.parametrize("root", ["worktrees", "clones"])
def test_unregistered_isolation_root_cannot_replace_primary_checkout(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    canonical_path = tmp_path / "canonical"
    canonical_path.mkdir()
    project = _seed_canonical_checkout(temp_db, canonical_path, name="orphan-project")
    orphaned_path = tmp_path / ".gobby" / root / "orphan-project" / "task-1"
    orphaned_path.mkdir(parents=True)

    with pytest.raises(IsolatedAgentProjectPathError, match="isolation path"):
        LocalProjectManager(temp_db).update(project.id, repo_path=str(orphaned_path))

    _assert_canonical_checkout(temp_db, project.id, canonical_path)


def test_isolation_root_check_fails_closed_when_candidate_resolution_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == candidate:
            raise PermissionError("candidate denied")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert LocalProjectManager._is_under_isolation_root(str(candidate)) is True


@pytest.mark.parametrize("root_name", ["worktrees", "clones"])
def test_isolation_root_check_fails_closed_when_root_resolution_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_name: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    candidate = tmp_path / "candidate"
    denied_root = tmp_path / ".gobby" / root_name
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == denied_root:
            raise PermissionError("root denied")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert LocalProjectManager._is_under_isolation_root(str(candidate)) is True


def test_nonisolated_init_can_register_missing_checkout(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    insert_isolated_machine(temp_db, LOCAL_MACHINE_ID)
    projects = LocalProjectManager(temp_db)
    project = projects.create(tmp_path.name)
    write_project_marker(tmp_path, project_id=project.id, name=project.name)

    result = initialize_project(tmp_path, db=temp_db)

    assert result.project_id == project.id
    assert result.project_path == str(tmp_path)
    _assert_canonical_checkout(temp_db, project.id, tmp_path)
