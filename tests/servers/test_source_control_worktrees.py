"""Client creation forks one selected commit from the requested project."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.source_control_worktrees import create_source_control_worktrees_router
from gobby.storage.worktrees import Worktree


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("base", [None, "main"])
def test_create_client_worktree_uses_exact_selected_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str | None
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Client Fork Test")
    git(repo, "commit", "--allow-empty", "-m", "main")
    main = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "caller")
    git(repo, "commit", "--allow-empty", "-m", "caller")
    selected = git(repo, "rev-parse", "HEAD") if base is None else main
    child = tmp_path / "child"
    server = MagicMock()
    server.run_db = AsyncMock(return_value=str(repo))
    storage = server.services.worktree_storage
    storage.get_by_branch.return_value = None
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    storage.create.side_effect = lambda **values: Worktree(
        id="fork",
        agent_session_id=None,
        status="active",
        created_at=stamp,
        updated_at=stamp,
        merged_at=None,
        **values,
    )
    monkeypatch.setattr("gobby.worktrees.creation.generate_worktree_path", lambda *args: str(child))
    monkeypatch.setattr("gobby.worktrees.events.emit_worktree_event", lambda *args, **kwargs: {})
    app = FastAPI()
    app.include_router(create_source_control_worktrees_router(server, validate_git_ref=MagicMock()))
    request = {"project_id": "project", "branch_name": "child", "workspace_role": "client"}
    if base is not None:
        request["base_branch"] = base
    with TestClient(app) as client:
        response = client.post("/worktrees", json=request)
    assert response.status_code == 200, response.text
    assert git(child, "rev-parse", "HEAD") == selected
    assert response.json()["base_branch"] == ("caller" if base is None else "main")
