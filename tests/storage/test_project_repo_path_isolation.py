"""Project repo path protection for managed isolated agent sessions."""

from __future__ import annotations

import json
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
from gobby.storage.projects import IsolatedAgentProjectPathError, LocalProjectManager, Project
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.project_context import ensure_project_json_for_isolation
from gobby.utils.project_init import initialize_project

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


@pytest.mark.parametrize("isolation", ["worktree", "clone"])
def test_isolated_agent_init_preserves_canonical_repo_path(
    temp_db: HubDatabase,
    tmp_path: Path,
    isolation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_path = tmp_path / "canonical"
    isolated_path = tmp_path / isolation
    canonical_path.mkdir()
    isolated_path.mkdir()

    projects = LocalProjectManager(temp_db)
    project = projects.create("shared-project", repo_path=str(canonical_path))
    project_file = canonical_path / ".gobby" / "project.json"
    project_file.parent.mkdir()
    project_file.write_text(
        json.dumps(
            {
                "id": project.id,
                "name": project.name,
                "created_at": project.created_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    ensure_project_json_for_isolation(canonical_path, isolated_path)
    child_session_id = _register_isolated_agent(
        temp_db,
        project=project,
        isolated_path=isolated_path,
        isolation=isolation,
    )

    monkeypatch.setenv("GOBBY_SESSION_ID", child_session_id)
    result = initialize_project(isolated_path, db=temp_db)

    refreshed = projects.get(project.id)
    assert refreshed is not None
    assert refreshed.repo_path == str(canonical_path)
    assert result.project_path == str(isolated_path)


@pytest.mark.parametrize("isolation", ["worktree", "clone"])
@pytest.mark.parametrize("variant", ["exact", "trailing-separator", "symlink"])
def test_hook_project_sync_preserves_canonical_repo_path_before_session_resolution(
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
    projects = LocalProjectManager(temp_db)
    project = projects.create("shared-project", repo_path=str(canonical_path))
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

    refreshed = projects.get(project.id)
    assert refreshed is not None
    assert refreshed.repo_path == str(canonical_path)


@pytest.mark.parametrize("isolation", ["worktree", "clone"])
@pytest.mark.parametrize("variant", ["exact", "trailing-separator", "symlink"])
def test_registered_isolation_path_cannot_explicitly_update_repo_path(
    temp_db: HubDatabase,
    tmp_path: Path,
    isolation: str,
    variant: str,
) -> None:
    canonical_path = tmp_path / "canonical"
    isolated_path = tmp_path / isolation
    canonical_path.mkdir()
    isolated_path.mkdir()
    projects = LocalProjectManager(temp_db)
    project = projects.create("shared-project", repo_path=str(canonical_path))
    _register_isolated_agent(
        temp_db,
        project=project,
        isolated_path=isolated_path,
        isolation=isolation,
    )
    repo_path = _isolation_path_variant(tmp_path, isolated_path, variant)

    with pytest.raises(IsolatedAgentProjectPathError, match="isolated agent session"):
        projects.update(project.id, repo_path=repo_path)

    refreshed = projects.get(project.id)
    assert refreshed is not None
    assert refreshed.repo_path == str(canonical_path)


def test_nonisolated_init_can_set_missing_repo_path(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    projects = LocalProjectManager(temp_db)
    project = projects.create(tmp_path.name)

    result = initialize_project(tmp_path, db=temp_db)

    refreshed = projects.get(project.id)
    assert refreshed is not None
    assert refreshed.repo_path == str(tmp_path)
    assert result.project_path == str(tmp_path)
