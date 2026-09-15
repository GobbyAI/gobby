"""Tests for source control API routes.

Exercises src/gobby/servers/routes/source_control.py endpoints and helper
functions using create_http_server() with mocked services.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

import gobby.servers.routes.source_control as sc_module
from gobby.clones.git import CloneGitManager
from gobby.servers.routes.source_control import (
    _get_cached,
    _set_cached,
    create_source_control_router,
)
from gobby.servers.routes.source_control import (
    _validate_git_ref as _validate_git_ref_impl,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutSentinelRejectedError
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    GLOBAL_PROJECT_ID,
    PERSONAL_PROJECT_ID,
    LocalProjectManager,
)
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.worktrees.executor import WorktreeDeleteExecutor
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    install_isolated_checkout_project,
    patch_local_machine_id,
)

# Inspect the runtime return contract without treating a None-returning call as a value.
_validate_git_ref: Callable[[str, str], object] = _validate_git_ref_impl

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    """Clear the module-level cache before and after every test."""
    sc_module._cache.clear()
    yield
    sc_module._cache.clear()


@pytest.fixture
def mock_server() -> Iterator[MagicMock]:
    """Create a mock HTTPServer with minimal service plumbing."""
    server = MagicMock()
    server.session_manager = MagicMock()
    server.session_manager.db = MagicMock()
    server.services = MagicMock()
    server.services.mcp_manager = None
    server.services.worktree_storage = None
    server.services.clone_storage = None
    server.services.git_manager = None
    server.services.task_manager = None
    server.services.terminal_manager = None
    executor = WorktreeDeleteExecutor()
    server.services.run_worktree_delete = executor.run_delete
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    try:
        yield server
    finally:
        executor.shutdown()
        executor.join()


@pytest.fixture
def client(mock_server: MagicMock) -> TestClient:
    """Create a TestClient with the source control router mounted."""
    app = FastAPI()
    router = create_source_control_router(mock_server)
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/api/source-control/prs",
        "/api/source-control/issues",
        "/api/source-control/cicd/runs",
    ],
)
def test_github_mcp_routes_are_unregistered(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_daemon_git_timeout_is_unavailable_without_blocking_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from gobby.servers.routes import source_control
    from gobby.utils.daemon_git import GitTimeout

    started = asyncio.Event()
    release = asyncio.Event()

    async def timeout_git(args: list[str], **kwargs: object) -> GitTimeout:
        started.set()
        await release.wait()
        return GitTimeout("timeout", ("git", *args), 0.01)

    monkeypatch.setattr(source_control, "_resolve_project", lambda *_args: ("/tmp/repo", None))
    monkeypatch.setattr("gobby.servers.routes.source_control_git.daemon_git.run", timeout_git)
    async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as http:
        request = asyncio.create_task(http.get("/api/source-control/status"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            heartbeat: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(heartbeat.set_result, True)
            assert await asyncio.wait_for(heartbeat, 1)
            assert not request.done()
        finally:
            release.set()
        response = await request
    assert response.status_code == 504
    assert response.json() == {"detail": "Git status timed out"}
    assert source_control._get_cached("status:default", 60) is None


# ---------------------------------------------------------------------------
# Helper: _validate_git_ref
# ---------------------------------------------------------------------------


class TestValidateGitRef:
    def test_valid_simple_branch(self) -> None:
        """A simple alphanumeric branch name should not raise."""
        result = _validate_git_ref("main", "branch")
        assert result is None

    def test_valid_slash_branch(self) -> None:
        result = _validate_git_ref("feature/my-branch", "branch")
        assert result is None

    def test_valid_with_dots(self) -> None:
        result = _validate_git_ref("v1.2.3", "tag")
        assert result is None

    def test_valid_with_hyphens_in_middle(self) -> None:
        result = _validate_git_ref("my-branch-name", "branch")
        assert result is None

    def test_valid_with_underscore(self) -> None:
        result = _validate_git_ref("release_candidate", "branch")
        assert result is None

    def test_invalid_empty_string(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("", "branch")
        assert exc_info.value.status_code == 400

    def test_invalid_double_dot(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("main..HEAD", "ref")
        assert exc_info.value.status_code == 400

    def test_invalid_starts_with_hyphen(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("-evil", "ref")
        assert exc_info.value.status_code == 400

    def test_invalid_shell_metachar_semicolon(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("main;rm -rf /", "ref")
        assert exc_info.value.status_code == 400

    def test_invalid_shell_metachar_backtick(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("main`whoami`", "ref")
        assert exc_info.value.status_code == 400

    def test_invalid_shell_metachar_dollar(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("$(evil)", "ref")
        assert exc_info.value.status_code == 400

    def test_invalid_space(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("my branch", "ref")
        assert exc_info.value.status_code == 400

    def test_invalid_pipe(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("main|cat /etc/passwd", "ref")
        assert exc_info.value.status_code == 400

    def test_error_message_contains_param_name(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_git_ref("", "my_param")
        assert "my_param" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Helper: _get_cached / _set_cached
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_miss_returns_none(self) -> None:
        assert _get_cached("nonexistent", 30.0) is None

    def test_cache_hit(self) -> None:
        _set_cached("key1", {"data": "value"})
        result = _get_cached("key1", 30.0)
        assert result == {"data": "value"}

    def test_cache_expired(self) -> None:
        """A cached value beyond TTL should return None."""
        sc_module._cache["expired_key"] = (time.time() - 100, {"old": True})
        result = _get_cached("expired_key", 30.0)
        assert result is None

    def test_cache_not_yet_expired(self) -> None:
        """A cached value within TTL should be returned."""
        sc_module._cache["fresh_key"] = (time.time() - 5, {"fresh": True})
        result = _get_cached("fresh_key", 30.0)
        assert result == {"fresh": True}

    def test_cache_eviction_when_full(self) -> None:
        """When cache reaches MAX_CACHE_SIZE, oldest quarter is evicted."""
        # Fill cache to the max
        base_time = time.time() - 1000
        for i in range(sc_module._MAX_CACHE_SIZE):
            sc_module._cache[f"key_{i}"] = (base_time + i, {"i": i})

        assert len(sc_module._cache) == sc_module._MAX_CACHE_SIZE

        # Insert one more should trigger eviction
        _set_cached("new_key", {"new": True})

        # Should have evicted _MAX_CACHE_SIZE // 4 entries, then added 1
        expected = sc_module._MAX_CACHE_SIZE - (sc_module._MAX_CACHE_SIZE // 4) + 1
        assert len(sc_module._cache) == expected

        # The new key should be present
        assert _get_cached("new_key", 30.0) == {"new": True}

        # The oldest keys should have been evicted
        evict_count = sc_module._MAX_CACHE_SIZE // 4
        for i in range(evict_count):
            assert f"key_{i}" not in sc_module._cache

    def test_set_cached_overwrites_existing(self) -> None:
        _set_cached("key", {"v": 1})
        _set_cached("key", {"v": 2})
        assert _get_cached("key", 30.0) == {"v": 2}


# ---------------------------------------------------------------------------
# Helper: _resolve_project
# ---------------------------------------------------------------------------


class TestResolveProject:
    def test_resolve_with_project_id(
        self, mock_server: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_project = MagicMock()
        mock_project.id = "proj-123"
        mock_project.github_repo = "owner/repo"

        mock_pm = MagicMock()
        mock_pm.get.return_value = mock_project
        mock_pm.db = MagicMock()
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_local_machine_id",
            lambda _provided, **_kwargs: "machine-1",
        )
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_root",
            lambda _db, _project_id, _machine_id: "/tmp/repo",
        )

        with patch(
            "gobby.servers.routes.source_control_git.LocalProjectManager", return_value=mock_pm
        ):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, "proj-123")

        assert repo_path == "/tmp/repo"
        assert github_repo == "owner/repo"

    def test_resolve_without_project_id_falls_back(
        self, mock_server: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_proj = MagicMock()
        mock_proj.id = "proj-fallback"
        mock_proj.name = "my-project"
        mock_proj.github_repo = "org/fallback"

        mock_pm = MagicMock()
        mock_pm.list.return_value = [mock_proj]
        mock_pm.db = MagicMock()
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_local_machine_id",
            lambda _provided, **_kwargs: "machine-1",
        )
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_root",
            lambda _db, _project_id, _machine_id: "/tmp/fallback",
        )

        with patch(
            "gobby.servers.routes.source_control_git.LocalProjectManager", return_value=mock_pm
        ):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, None)

        assert repo_path == "/tmp/fallback"
        assert github_repo == "org/fallback"

    def test_resolve_skips_hidden_projects(
        self, mock_server: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orphaned = MagicMock()
        orphaned.id = "orphaned"
        orphaned.name = "_orphaned"

        real = MagicMock()
        real.id = "real"
        real.name = "real-project"
        real.github_repo = None

        mock_pm = MagicMock()
        mock_pm.list.return_value = [orphaned, real]
        mock_pm.db = MagicMock()
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_local_machine_id",
            lambda _provided, **_kwargs: "machine-1",
        )
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_root",
            lambda _db, project_id, _machine_id: "/tmp/real"
            if project_id == "real"
            else "/tmp/orphaned",
        )

        with patch(
            "gobby.servers.routes.source_control_git.LocalProjectManager", return_value=mock_pm
        ):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, None)

        assert repo_path == "/tmp/real"
        assert github_repo is None

    def test_resolve_returns_none_none_on_failure(self, mock_server: MagicMock) -> None:
        mock_server.session_manager = None

        from gobby.servers.routes.source_control import _resolve_project

        repo_path, github_repo = _resolve_project(mock_server, "proj-123")
        assert repo_path is None
        assert github_repo is None

    def test_resolve_fallback_skips_checkout_free_sentinels(
        self, mock_server: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_project = MagicMock()
        global_project.id = GLOBAL_PROJECT_ID
        global_project.name = "_global"
        personal = MagicMock()
        personal.id = PERSONAL_PROJECT_ID
        personal.name = "_personal"
        real = MagicMock()
        real.id = "real"
        real.name = "real-project"
        real.github_repo = "org/real"

        mock_pm = MagicMock()
        mock_pm.list.return_value = [global_project, personal, real]
        mock_pm.db = MagicMock()
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_local_machine_id",
            lambda _provided, **_kwargs: "machine-1",
        )
        resolved: list[str] = []

        def fake_require_root(_db: Any, project_id: str, _machine_id: str) -> str:
            resolved.append(project_id)
            if project_id in CHECKOUT_FREE_PROJECT_IDS:
                raise CheckoutSentinelRejectedError(project_id)
            return "/tmp/real"

        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git.require_root", fake_require_root
        )

        with patch(
            "gobby.servers.routes.source_control_git.LocalProjectManager", return_value=mock_pm
        ):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, None)

        assert (repo_path, github_repo) == ("/tmp/real", "org/real")
        assert resolved == ["real"]

    def test_resolve_explicit_sentinel_is_empty_not_conflict(
        self, mock_server: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        personal = MagicMock()
        personal.id = PERSONAL_PROJECT_ID
        personal.name = "_personal"
        personal.github_repo = None

        mock_pm = MagicMock()
        mock_pm.get.return_value = personal
        mock_pm.db = MagicMock()

        def refuse(*_args: Any, **_kwargs: Any) -> str:
            raise AssertionError("sentinel projects must not resolve a checkout")

        monkeypatch.setattr("gobby.servers.routes.source_control_git.require_root", refuse)

        with patch(
            "gobby.servers.routes.source_control_git.LocalProjectManager", return_value=mock_pm
        ):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, PERSONAL_PROJECT_ID)

        assert repo_path is None
        assert github_repo is None

    def test_status_for_sentinel_project_is_empty_200(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        personal = MagicMock()
        personal.id = PERSONAL_PROJECT_ID
        personal.name = "_personal"
        personal.github_repo = None
        mock_pm = MagicMock()
        mock_pm.get.return_value = personal
        mock_pm.db = MagicMock()
        with patch(
            "gobby.servers.routes.source_control_git.LocalProjectManager", return_value=mock_pm
        ):
            response = client.get(
                "/api/source-control/status", params={"project_id": PERSONAL_PROJECT_ID}
            )

        assert response.status_code == 200
        assert response.json() == {
            "github_repo": None,
            "current_branch": None,
            "branch_count": 0,
            "worktree_count": 0,
            "clone_count": 0,
            "repo_path": None,
            "ahead": None,
            "behind": None,
        }


# ---------------------------------------------------------------------------
# Helper: _get_project_manager
# ---------------------------------------------------------------------------


class TestGetProjectManager:
    def test_raises_503_when_session_manager_is_none(self, mock_server: MagicMock) -> None:
        mock_server.session_manager = None

        from gobby.servers.routes.source_control import _get_project_manager

        with pytest.raises(HTTPException) as exc_info:
            _get_project_manager(mock_server)
        assert exc_info.value.status_code == 503

    def test_returns_project_manager(self, mock_server: MagicMock) -> None:
        with patch("gobby.servers.routes.source_control_git.LocalProjectManager") as mock_cls:
            from gobby.servers.routes.source_control import _get_project_manager

            _get_project_manager(mock_server)
            mock_cls.assert_called_once_with(mock_server.session_manager.db)
            assert mock_cls.call_count == 1
            assert mock_cls.call_args is not None


# ---------------------------------------------------------------------------
# GET /api/source-control/status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_status_no_repo_path(self, client: TestClient, mock_server: MagicMock) -> None:
        """When no project resolves, returns minimal status."""
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/status")

        assert response.status_code == 200
        data = response.json()
        assert data["current_branch"] is None
        assert data["branch_count"] == 0

    def test_status_with_repo_path(self, client: TestClient, mock_server: MagicMock) -> None:
        """When repo_path resolves, runs git commands to get branch info."""
        mock_server.services.worktree_storage = None
        mock_server.services.clone_storage = None

        # Mock git responses
        branch_result = MagicMock(returncode=0, stdout="feature/test\n")
        list_result = MagicMock(returncode=0, stdout="  main\n* feature/test\n  develop\n")
        tracking_result = MagicMock(returncode=0, stdout="\t\n")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[branch_result, list_result, tracking_result],
            ),
        ):
            response = client.get("/api/source-control/status")

        assert response.status_code == 200
        data = response.json()
        assert data["current_branch"] == "feature/test"
        assert data["branch_count"] == 3

    def test_status_with_worktree_and_clone_counts(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_wt_storage = MagicMock()
        mock_wt_storage.list_worktrees.return_value = [MagicMock(), MagicMock()]
        mock_server.services.worktree_storage = mock_wt_storage

        mock_clone_storage = MagicMock()
        mock_clone_storage.list_clones.return_value = [MagicMock()]
        mock_server.services.clone_storage = mock_clone_storage

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, None),
            ),
        ):
            response = client.get("/api/source-control/status")

        assert response.status_code == 200
        data = response.json()
        assert data["worktree_count"] == 2
        assert data["clone_count"] == 1


# ---------------------------------------------------------------------------
# GET /api/source-control/branches
# ---------------------------------------------------------------------------


class TestListBranches:
    def test_branches_no_repo(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/branches")

        assert response.status_code == 200
        data = response.json()
        assert data["branches"] == []
        assert data["current_branch"] is None

    def test_branches_with_data(self, client: TestClient, mock_server: MagicMock) -> None:
        """Local branches with ahead/behind info are parsed correctly."""
        mock_server.services.worktree_storage = None

        current_result = MagicMock(returncode=0, stdout="main\n")
        local_result = MagicMock(
            returncode=0,
            stdout=(
                "main\torigin/main\t[ahead 2, behind 1]\t2025-01-01T00:00:00+00:00\n"
                "feature\torigin/feature\t[ahead 3]\t2025-01-02T00:00:00+00:00\n"
            ),
        )
        remote_result = MagicMock(
            returncode=0,
            stdout="origin/develop\t2025-01-03T00:00:00+00:00\n",
        )

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[current_result, local_result, remote_result],
            ),
        ):
            response = client.get("/api/source-control/branches")

        assert response.status_code == 200
        data = response.json()
        assert data["current_branch"] == "main"

        branches = data["branches"]
        assert len(branches) == 3

        main_branch = next(b for b in branches if b["name"] == "main")
        assert main_branch["is_current"] is True
        assert main_branch["ahead"] == 2
        assert main_branch["behind"] == 1
        assert main_branch["is_remote"] is False

        feature_branch = next(b for b in branches if b["name"] == "feature")
        assert feature_branch["ahead"] == 3
        assert feature_branch["behind"] == 0

        develop_branch = next(b for b in branches if b["name"] == "develop")
        assert develop_branch["is_remote"] is True

    def test_branches_cached(self, client: TestClient, mock_server: MagicMock) -> None:
        """Second call returns cached result without running git."""
        mock_server.services.worktree_storage = None

        current_result = MagicMock(returncode=0, stdout="main\n")
        local_result = MagicMock(returncode=0, stdout="main\t\t\t2025-01-01\n")
        remote_result = MagicMock(returncode=0, stdout="\n")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[current_result, local_result, remote_result],
            ) as mock_git,
        ):
            response1 = client.get("/api/source-control/branches")
            assert response1.status_code == 200

            # Second call should use cache - no additional git calls
            response2 = client.get("/api/source-control/branches")
            assert response2.status_code == 200

            # _run_git should only be called for the first request (3 calls)
            assert mock_git.call_count == 3

    def test_branches_does_not_cache_partial_result_after_git_failure(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """A partial branch list is retried after an expected git failure."""
        mock_server.services.worktree_storage = None

        current_result = MagicMock(returncode=0, stdout="main\n")
        local_result = MagicMock(returncode=0, stdout="main\t\t\t2025-01-01\n")
        remote_result = MagicMock(
            returncode=0,
            stdout="origin/develop\t2025-01-02\n",
        )

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[
                    current_result,
                    local_result,
                    subprocess.TimeoutExpired("git", 15),
                    current_result,
                    local_result,
                    remote_result,
                ],
            ) as mock_git,
        ):
            partial_response = client.get("/api/source-control/branches")
            retried_response = client.get("/api/source-control/branches")

        assert partial_response.status_code == 200
        assert [branch["name"] for branch in partial_response.json()["branches"]] == ["main"]
        assert retried_response.status_code == 200
        assert [branch["name"] for branch in retried_response.json()["branches"]] == [
            "main",
            "develop",
        ]
        assert mock_git.call_count == 6

    def test_branches_skips_remote_head(self, client: TestClient, mock_server: MagicMock) -> None:
        """origin/HEAD should be excluded from remote branches."""
        mock_server.services.worktree_storage = None

        current_result = MagicMock(returncode=0, stdout="main\n")
        local_result = MagicMock(returncode=0, stdout="main\t\t\t2025-01-01\n")
        remote_result = MagicMock(
            returncode=0,
            stdout="origin/HEAD\t2025-01-01\norigin/other\t2025-01-02\n",
        )

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[current_result, local_result, remote_result],
            ),
        ):
            response = client.get("/api/source-control/branches")

        branches = response.json()["branches"]
        names = [b["name"] for b in branches]
        assert "HEAD" not in names
        assert "other" in names

    def test_branches_skips_duplicate_remote(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Remote branches that match local branches are excluded."""
        mock_server.services.worktree_storage = None

        current_result = MagicMock(returncode=0, stdout="main\n")
        local_result = MagicMock(returncode=0, stdout="main\t\t\t2025-01-01\n")
        remote_result = MagicMock(
            returncode=0,
            stdout="origin/main\t2025-01-01\n",
        )

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[current_result, local_result, remote_result],
            ),
        ):
            response = client.get("/api/source-control/branches")

        branches = response.json()["branches"]
        # Only the local "main", not the remote duplicate
        assert len(branches) == 1
        assert branches[0]["is_remote"] is False


# ---------------------------------------------------------------------------
# POST /api/source-control/branches/checkout
# ---------------------------------------------------------------------------


class TestCheckoutBranch:
    def test_checkout_success_switches_existing_local_branch(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        show_ref_result = MagicMock(returncode=0, stdout="")
        switch_result = MagicMock(returncode=0, stdout="", stderr="")
        current_result = MagicMock(returncode=0, stdout="main\n")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[show_ref_result, switch_result, current_result],
            ) as mock_git,
        ):
            response = client.post(
                "/api/source-control/branches/checkout",
                json={"branch_name": "main"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data == {
            "success": True,
            "current_branch": "main",
            "repo_path": "/tmp/repo",
        }
        assert mock_git.await_args_list == [
            call(["show-ref", "--verify", "refs/heads/main"], "/tmp/repo"),
            call(["switch", "main"], "/tmp/repo", timeout=30),
            call(["branch", "--show-current"], "/tmp/repo"),
        ]

    def test_checkout_invalid_ref_returns_400(self, client: TestClient) -> None:
        response = client.post(
            "/api/source-control/branches/checkout",
            json={"branch_name": "..evil"},
        )

        assert response.status_code == 400

    def test_checkout_no_repo_returns_400(self, client: TestClient) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.post(
                "/api/source-control/branches/checkout",
                json={"branch_name": "main"},
            )

        assert response.status_code == 400

    def test_checkout_missing_local_branch_returns_404(self, client: TestClient) -> None:
        show_ref_result = MagicMock(returncode=1, stdout="", stderr="")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                return_value=show_ref_result,
            ) as mock_git,
        ):
            response = client.post(
                "/api/source-control/branches/checkout",
                json={"branch_name": "feature"},
            )

        assert response.status_code == 404
        mock_git.assert_awaited_once_with(
            ["show-ref", "--verify", "refs/heads/feature"],
            "/tmp/repo",
        )

    def test_checkout_failed_switch_returns_409(self, client: TestClient) -> None:
        show_ref_result = MagicMock(returncode=0, stdout="")
        switch_result = MagicMock(returncode=1, stdout="", stderr="dirty worktree\n")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[show_ref_result, switch_result],
            ),
        ):
            response = client.post(
                "/api/source-control/branches/checkout",
                json={"branch_name": "feature"},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "dirty worktree"

    def test_checkout_success_invalidates_branch_cache(self, client: TestClient) -> None:
        sc_module._cache["branches:proj-1"] = (time.time(), {"branches": ["stale"]})
        sc_module._cache["prs:owner/repo:open"] = (time.time(), {"prs": ["cached"]})

        show_ref_result = MagicMock(returncode=0, stdout="")
        switch_result = MagicMock(returncode=0, stdout="", stderr="")
        current_result = MagicMock(returncode=0, stdout="feature\n")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[show_ref_result, switch_result, current_result],
            ),
        ):
            response = client.post(
                "/api/source-control/branches/checkout?project_id=proj-1",
                json={"branch_name": "feature"},
            )

        assert response.status_code == 200
        assert "branches:proj-1" not in sc_module._cache
        assert "prs:owner/repo:open" in sc_module._cache


# ---------------------------------------------------------------------------
# GET /api/source-control/branches/{branch_name}/commits
# ---------------------------------------------------------------------------


class TestListBranchCommits:
    def test_commits_valid_branch(self, client: TestClient, mock_server: MagicMock) -> None:
        git_output = (
            "abc123full\tabc123\tFirst commit\tAlice\t2025-01-01T00:00:00+00:00\n"
            "def456full\tdef456\tSecond commit\tBob\t2025-01-02T00:00:00+00:00\n"
        )
        git_result = MagicMock(returncode=0, stdout=git_output)

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                return_value=git_result,
            ),
        ):
            response = client.get("/api/source-control/branches/main/commits")

        assert response.status_code == 200
        commits = response.json()["commits"]
        assert len(commits) == 2
        assert commits[0]["sha"] == "abc123full"
        assert commits[0]["short_sha"] == "abc123"
        assert commits[0]["message"] == "First commit"
        assert commits[0]["author"] == "Alice"

    def test_commits_no_repo(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/branches/main/commits")

        assert response.status_code == 200
        assert response.json()["commits"] == []

    def test_commits_invalid_branch_name(self, client: TestClient, mock_server: MagicMock) -> None:
        """Branch names with shell metacharacters are rejected."""
        response = client.get("/api/source-control/branches/;rm -rf/commits")
        assert response.status_code == 400

    def test_commits_branch_with_slashes(self, client: TestClient, mock_server: MagicMock) -> None:
        """Branch names like feature/foo should work with path param."""
        git_result = MagicMock(returncode=0, stdout="")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                return_value=git_result,
            ),
        ):
            response = client.get("/api/source-control/branches/feature/my-branch/commits")

        assert response.status_code == 200

    def test_commits_with_limit(self, client: TestClient, mock_server: MagicMock) -> None:
        git_result = MagicMock(returncode=0, stdout="")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                return_value=git_result,
            ) as mock_git,
        ):
            response = client.get("/api/source-control/branches/main/commits?limit=5")

        assert response.status_code == 200
        # Verify the limit is passed through (capped at 100)
        call_args = mock_git.call_args[0][0]
        assert "--max-count=5" in call_args

    @pytest.mark.parametrize("limit", [-1, 101])
    def test_commits_rejects_out_of_range_limit(self, client: TestClient, limit: int) -> None:
        response = client.get(f"/api/source-control/branches/main/commits?limit={limit}")

        assert response.status_code == 422

    def test_commits_uses_git_when_github_repo_and_mcp_are_configured(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_server.services.mcp_manager = MagicMock()
        git_result = MagicMock(returncode=0, stdout="")
        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                return_value=git_result,
            ) as mock_git,
        ):
            response = client.get("/api/source-control/branches/main/commits")

        assert response.status_code == 200
        assert response.json() == {"commits": []}
        mock_git.assert_awaited_once()
        mock_server.services.mcp_manager.assert_not_called()

    @pytest.mark.parametrize("limit", [1, 100])
    def test_commits_accepts_boundary_limit(
        self, client: TestClient, mock_server: MagicMock, limit: int
    ) -> None:
        git_result = MagicMock(returncode=0, stdout="")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                return_value=git_result,
            ) as mock_git,
        ):
            response = client.get(f"/api/source-control/branches/main/commits?limit={limit}")

        assert response.status_code == 200
        assert f"--max-count={limit}" in mock_git.call_args.args[0]


# ---------------------------------------------------------------------------
# GET /api/source-control/diff
# ---------------------------------------------------------------------------


class TestGetDiff:
    def test_diff_with_valid_refs(self, client: TestClient, mock_server: MagicMock) -> None:
        stat_result = MagicMock(returncode=0, stdout=" file.py | 10 ++++\n")
        files_result = MagicMock(returncode=0, stdout="M\tfile.py\nA\tnew.py\n")
        patch_result = MagicMock(returncode=0, stdout="diff --git a/file.py b/file.py\n...")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[stat_result, files_result, patch_result],
            ),
        ):
            response = client.get("/api/source-control/diff?base=main&head=feature")

        assert response.status_code == 200
        data = response.json()
        assert "file.py" in data["diff_stat"]
        assert len(data["files"]) == 2
        assert data["files"][0]["status"] == "M"
        assert data["files"][0]["path"] == "file.py"
        assert "diff --git" in data["patch"]

    def test_diff_no_repo_path(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/diff")

        assert response.status_code == 400

    def test_diff_invalid_base_ref(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.get("/api/source-control/diff?base=;evil&head=HEAD")
        assert response.status_code == 400

    def test_diff_invalid_head_ref(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.get("/api/source-control/diff?base=main&head=..evil")
        assert response.status_code == 400

    def test_diff_timeout(self, client: TestClient, mock_server: MagicMock) -> None:
        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
            ),
        ):
            response = client.get("/api/source-control/diff")

        assert response.status_code == 504

    def test_diff_general_error(self, client: TestClient, mock_server: MagicMock) -> None:
        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=OSError("git not found"),
            ),
        ):
            response = client.get("/api/source-control/diff")

        assert response.status_code == 500

    def test_diff_patch_truncated(self, client: TestClient, mock_server: MagicMock) -> None:
        """Patch output is truncated to MAX_PATCH_BYTES."""
        large_patch = "x" * 200_000
        stat_result = MagicMock(returncode=0, stdout="")
        files_result = MagicMock(returncode=0, stdout="")
        patch_result = MagicMock(returncode=0, stdout=large_patch)

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[stat_result, files_result, patch_result],
            ),
        ):
            response = client.get("/api/source-control/diff")

        assert response.status_code == 200
        assert len(response.json()["patch"]) == sc_module.MAX_PATCH_BYTES


# ---------------------------------------------------------------------------
# GET /api/source-control/worktrees
# ---------------------------------------------------------------------------


class TestListWorktrees:
    def test_worktrees_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.worktree_storage = None
        response = client.get("/api/source-control/worktrees")
        assert response.status_code == 200
        assert response.json()["worktrees"] == []

    def test_worktrees_with_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        wt1 = MagicMock()
        wt1.to_dict.return_value = {"id": "wt-1", "branch_name": "feature"}
        wt2 = MagicMock()
        wt2.to_dict.return_value = {"id": "wt-2", "branch_name": "bugfix"}

        mock_storage = MagicMock()
        mock_storage.list_worktrees.return_value = [wt1, wt2]
        mock_server.services.worktree_storage = mock_storage

        response = client.get("/api/source-control/worktrees")
        assert response.status_code == 200
        data = response.json()
        assert len(data["worktrees"]) == 2
        assert data["worktrees"][0]["id"] == "wt-1"

    def test_worktrees_with_project_id_filter(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_storage = MagicMock()
        mock_storage.list_worktrees.return_value = []
        mock_server.services.worktree_storage = mock_storage

        response = client.get("/api/source-control/worktrees?project_id=proj-1")
        assert response.status_code == 200
        mock_storage.list_worktrees.assert_called_once_with(project_id="proj-1", status=None)

    def test_worktrees_with_status_filter(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_storage.list_worktrees.return_value = []
        mock_server.services.worktree_storage = mock_storage

        response = client.get("/api/source-control/worktrees?status=active")
        assert response.status_code == 200
        mock_storage.list_worktrees.assert_called_once_with(project_id=None, status="active")


# ---------------------------------------------------------------------------
# GET /api/source-control/worktrees/stats
# ---------------------------------------------------------------------------


class TestWorktreeStats:
    def test_stats_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.worktree_storage = None
        response = client.get("/api/source-control/worktrees/stats")
        assert response.status_code == 200
        assert response.json()["stats"] == {}

    def test_stats_no_project_id(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_server.services.worktree_storage = mock_storage

        response = client.get("/api/source-control/worktrees/stats")
        assert response.status_code == 200
        assert response.json()["stats"] == {}

    def test_stats_with_project_id(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_storage.count_by_status.return_value = {"active": 3, "stale": 1}
        mock_server.services.worktree_storage = mock_storage

        response = client.get("/api/source-control/worktrees/stats?project_id=proj-1")
        assert response.status_code == 200
        assert response.json()["stats"] == {"active": 3, "stale": 1}


# ---------------------------------------------------------------------------
# DELETE /api/source-control/worktrees/{worktree_id}
# ---------------------------------------------------------------------------


class TestDeleteWorktree:
    def test_foreign_worktree_returns_conflict_without_effects(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_storage = MagicMock()
        mock_storage.get.side_effect = MachineOwnershipMismatchError(
            resource_kind="worktree",
            resource_id="10000000-0000-4000-8000-000000000001",
            owner_machine_id="20000000-0000-4000-8000-000000000002",
            current_machine_id="20000000-0000-4000-8000-000000000001",
        )
        mock_server.services.worktree_storage = mock_storage
        mock_server.services.git_manager = MagicMock()

        response = client.delete(
            "/api/source-control/worktrees/10000000-0000-4000-8000-000000000001"
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "machine_ownership_mismatch"
        mock_server.services.git_manager.delete_worktree.assert_not_called()
        mock_storage.delete.assert_not_called()

    def test_delete_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.worktree_storage = None
        response = client.delete("/api/source-control/worktrees/wt-1")
        assert response.status_code == 503

    def test_delete_not_found(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.worktree_storage = mock_storage

        response = client.delete("/api/source-control/worktrees/wt-999")
        assert response.status_code == 404

    def test_delete_success_no_git_manager(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.project_id = "proj-1"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_storage.delete.return_value = True
        mock_server.services.worktree_storage = mock_storage
        mock_server.services.git_manager = None

        response = client.delete("/api/source-control/worktrees/wt-1")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == "wt-1"
        assert data["git_deleted"] is True  # defaults to True when no git_manager

    @pytest.mark.parametrize("merged_into", [None, "0.5.0"])
    def test_delete_success_with_git_manager(
        self, client: TestClient, mock_server: MagicMock, merged_into: str | None
    ) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.project_id = "proj-1"
        wt.branch_name = "feature"
        wt.base_branch = "main"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_storage.delete.return_value = True
        mock_server.services.worktree_storage = mock_storage

        mock_artifacts = MagicMock()
        mock_artifacts.clear_worktree_references.return_value = 2
        mock_server.services.task_manager = SimpleNamespace(artifacts=mock_artifacts)

        mock_git_result = MagicMock()
        mock_git_result.success = True
        mock_git_manager = MagicMock()
        mock_server.services.git_manager = mock_git_manager

        with (
            patch(
                "gobby.servers.routes.source_control_git._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.worktrees.git.WorktreeGitManager",
            ) as mock_wgm_cls,
            patch("gobby.worktrees.deletion.emit_worktree_event") as emit_event,
        ):
            mock_wgm_cls.return_value.delete_worktree = AsyncMock(return_value=mock_git_result)

            response = client.delete(
                "/api/source-control/worktrees/wt-1",
                params={"merged_into": merged_into} if merged_into is not None else None,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["git_deleted"] is True
        mock_wgm_cls.return_value.delete_worktree.assert_called_once_with(
            "/tmp/wt",
            force=True,
            delete_branch=True,
            force_delete_branch=False,
            branch_name="feature",
            base_branch="main",
            merged_into=merged_into,
        )
        mock_storage.delete.assert_called_once_with("wt-1")
        mock_artifacts.clear_worktree_references.assert_called_once_with("wt-1")
        emit_event.assert_called_once_with(
            "worktree_deleted",
            worktree_id="wt-1",
            project_id="proj-1",
            branch_name="feature",
            worktree_path="/tmp/wt",
            artifact_refs_cleared=2,
        )

    def test_delete_git_deletion_fails(self, client: TestClient, mock_server: MagicMock) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.project_id = "proj-1"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_storage.delete.return_value = True
        mock_server.services.worktree_storage = mock_storage

        mock_git_result = MagicMock()
        mock_git_result.success = False
        mock_git_result.message = "worktree locked"
        mock_git_manager = MagicMock()
        mock_server.services.git_manager = mock_git_manager

        with (
            patch(
                "gobby.servers.routes.source_control_git._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.worktrees.git.WorktreeGitManager",
            ) as mock_wgm_cls,
        ):
            mock_wgm_cls.return_value.delete_worktree = AsyncMock(return_value=mock_git_result)

            response = client.delete("/api/source-control/worktrees/wt-1")

        assert response.status_code == 409
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["git_deleted"] is False
        assert data["git_error"] == "worktree locked"
        assert data["message"] == "Git worktree deletion failed; DB record was preserved"
        mock_storage.delete.assert_not_called()

    def test_delete_git_deletion_raises(self, client: TestClient, mock_server: MagicMock) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.project_id = "proj-1"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_server.services.worktree_storage = mock_storage
        mock_server.services.git_manager = MagicMock()

        with (
            patch(
                "gobby.servers.routes.source_control_git._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.worktrees.git.WorktreeGitManager",
            ) as mock_wgm_cls,
        ):
            mock_wgm_cls.return_value.delete_worktree.side_effect = RuntimeError("git failed")

            response = client.delete("/api/source-control/worktrees/wt-1")

        assert response.status_code == 409
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["git_deleted"] is False
        assert data["git_error"] == "git failed"
        assert data["message"] == "Git worktree deletion failed; DB record was preserved"
        mock_storage.delete.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/source-control/worktrees/cleanup
# ---------------------------------------------------------------------------


class TestCleanupWorktrees:
    def test_cleanup_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.worktree_storage = None
        response = client.post("/api/source-control/worktrees/cleanup")
        assert response.status_code == 200
        assert response.json()["candidates"] == []

    def test_cleanup_no_project_id(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_server.services.worktree_storage = mock_storage

        response = client.post("/api/source-control/worktrees/cleanup")
        assert response.status_code == 200
        assert response.json()["candidates"] == []

    def test_cleanup_dry_run(self, client: TestClient, mock_server: MagicMock) -> None:
        stale_wt = MagicMock()
        stale_wt.to_dict.return_value = {"id": "wt-old", "status": "stale"}

        mock_storage = MagicMock()
        mock_storage.cleanup_stale.return_value = [stale_wt]
        mock_server.services.worktree_storage = mock_storage

        response = client.post(
            "/api/source-control/worktrees/cleanup?project_id=proj-1&dry_run=true"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["candidates"]) == 1
        assert data["cleaned"] == 0
        assert data["dry_run"] is True

    def test_cleanup_execute(self, client: TestClient, mock_server: MagicMock) -> None:
        stale_wt = MagicMock()
        stale_wt.to_dict.return_value = {"id": "wt-old"}

        mock_storage = MagicMock()
        mock_storage.cleanup_stale.return_value = [stale_wt]
        mock_server.services.worktree_storage = mock_storage

        response = client.post(
            "/api/source-control/worktrees/cleanup?project_id=proj-1&dry_run=false"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["cleaned"] == 1
        assert data["dry_run"] is False


# ---------------------------------------------------------------------------
# POST /api/source-control/worktrees/{worktree_id}/sync
# ---------------------------------------------------------------------------


class TestSyncWorktree:
    def test_foreign_worktree_returns_conflict_before_sync(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_storage = MagicMock()
        mock_storage.get.side_effect = MachineOwnershipMismatchError(
            resource_kind="worktree",
            resource_id="10000000-0000-4000-8000-000000000001",
            owner_machine_id="20000000-0000-4000-8000-000000000002",
            current_machine_id="20000000-0000-4000-8000-000000000001",
        )
        mock_server.services.worktree_storage = mock_storage
        mock_server.services.git_manager = MagicMock()

        response = client.post(
            "/api/source-control/worktrees/10000000-0000-4000-8000-000000000001/sync"
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "machine_ownership_mismatch"
        mock_server.services.git_manager.sync_from_main.assert_not_called()

    def test_sync_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.worktree_storage = None
        response = client.post("/api/source-control/worktrees/wt-1/sync")
        assert response.status_code == 503

    def test_sync_not_found(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.worktree_storage = mock_storage

        response = client.post("/api/source-control/worktrees/wt-999/sync")
        assert response.status_code == 404

    @pytest.mark.parametrize("success", [True, False])
    def test_sync_with_git_manager(
        self, client: TestClient, mock_server: MagicMock, success: bool
    ) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.base_branch = "main"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_server.services.worktree_storage = mock_storage

        mock_result = MagicMock()
        mock_result.success = success
        mock_result.message = "Synced successfully" if success else "Merge conflict"
        mock_git = MagicMock()
        mock_git.sync_from_main = AsyncMock(return_value=mock_result)
        mock_server.services.git_manager = mock_git

        response = client.post("/api/source-control/worktrees/wt-1/sync")
        assert response.status_code == (200 if success else 409)
        data = response.json() if success else response.json()["detail"]
        assert data["success"] is success
        assert data["message"] == mock_result.message
        assert data["id"] == "wt-1"

    def test_sync_without_git_manager_returns_unavailable(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.base_branch = "main"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_server.services.worktree_storage = mock_storage
        mock_server.services.git_manager = None

        response = client.post("/api/source-control/worktrees/wt-1/sync")
        assert response.status_code == 503
        assert response.json()["detail"] == "Git manager not available"


# ---------------------------------------------------------------------------
# GET /api/source-control/clones
# ---------------------------------------------------------------------------


class TestListClones:
    def test_clones_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.clone_storage = None
        response = client.get("/api/source-control/clones")
        assert response.status_code == 200
        assert response.json()["clones"] == []

    def test_clones_with_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        c1 = MagicMock()
        c1.to_dict.return_value = {"id": "clone-1", "status": "active"}
        c2 = MagicMock()
        c2.to_dict.return_value = {"id": "clone-2", "status": "active"}

        mock_storage = MagicMock()
        mock_storage.list_clones.return_value = [c1, c2]
        mock_server.services.clone_storage = mock_storage

        response = client.get("/api/source-control/clones")
        assert response.status_code == 200
        data = response.json()
        assert len(data["clones"]) == 2
        assert data["clones"][0]["id"] == "clone-1"

    def test_clones_with_project_id(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_storage.list_clones.return_value = []
        mock_server.services.clone_storage = mock_storage

        response = client.get("/api/source-control/clones?project_id=proj-1")
        assert response.status_code == 200
        mock_storage.list_clones.assert_called_once_with(project_id="proj-1")


# ---------------------------------------------------------------------------
# DELETE /api/source-control/clones/{clone_id}
# ---------------------------------------------------------------------------


@pytest.fixture
def clone_git(mock_server: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    clone = SimpleNamespace(
        id="clone-1",
        clone_path=str(tmp_path),
        project_id="project-1",
        branch_name="feature/test",
        status="active",
    )
    mock_server.services.clone_storage = MagicMock()
    mock_server.services.clone_storage.get.return_value = clone
    mock_server.services.clone_storage.delete.return_value = True
    manager = MagicMock(spec=CloneGitManager)
    manager.delete_clone.return_value = SimpleNamespace(success=True)
    manager.sync_clone.return_value = SimpleNamespace(success=True)
    monkeypatch.setattr(
        "gobby.servers.routes.source_control_worktrees.CloneGitManager", lambda _path: manager
    )
    monkeypatch.setattr(
        "gobby.servers.routes.source_control_git._resolve_project",
        lambda _server, _project_id: (str(tmp_path), None),
    )
    return manager


class TestDeleteClone:
    def test_delete_missing_managed_path_retries_record_removal(
        self,
        client: TestClient,
        mock_server: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        clones_root = tmp_path / "clones"
        clones_root.mkdir()
        clone_path = clones_root / "already-deleted"
        clone = SimpleNamespace(
            id="clone-1", clone_path=str(clone_path), project_id="project-1", status="active"
        )
        storage = MagicMock()
        storage.get.return_value = clone
        storage.delete.side_effect = [False, True]
        mock_server.services.clone_storage = storage
        monkeypatch.setattr("gobby.clones.git.CLONES_ROOT", clones_root)
        monkeypatch.setattr(
            "gobby.servers.routes.source_control_git._resolve_project",
            lambda _server, _project_id: (str(tmp_path), None),
        )

        first = client.delete("/api/source-control/clones/clone-1")
        assert first.status_code == 409
        second = client.delete("/api/source-control/clones/clone-1")
        assert second.status_code == 200
        assert second.json()["success"] is True
        assert storage.delete.call_count == 2
        assert not clone_path.exists()

    def test_foreign_clone_returns_conflict_without_effects(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_storage = MagicMock()
        mock_storage.get.side_effect = MachineOwnershipMismatchError(
            resource_kind="clone",
            resource_id="10000000-0000-4000-8000-000000000002",
            owner_machine_id="20000000-0000-4000-8000-000000000002",
            current_machine_id="20000000-0000-4000-8000-000000000001",
        )
        mock_server.services.clone_storage = mock_storage

        response = client.delete("/api/source-control/clones/10000000-0000-4000-8000-000000000002")

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "machine_ownership_mismatch"
        mock_storage.delete.assert_not_called()

    def test_delete_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.clone_storage = None
        response = client.delete("/api/source-control/clones/clone-1")
        assert response.status_code == 503

    def test_delete_not_found(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.clone_storage = mock_storage

        response = client.delete("/api/source-control/clones/clone-999")
        assert response.status_code == 404

    def test_delete_success(
        self, client: TestClient, mock_server: MagicMock, clone_git: MagicMock
    ) -> None:
        response = client.delete("/api/source-control/clones/clone-1")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == "clone-1"
        clone_git.delete_clone.assert_awaited_once_with(
            mock_server.services.clone_storage.get.return_value.clone_path, force=False
        )

    def test_delete_returns_false(
        self, client: TestClient, mock_server: MagicMock, clone_git: MagicMock
    ) -> None:
        """A failed record removal must not look successful to HTTP clients."""
        mock_server.services.clone_storage.delete.return_value = False

        response = client.delete("/api/source-control/clones/clone-1")
        assert response.status_code == 409
        assert response.json()["detail"]["success"] is False
        clone_git.delete_clone.assert_awaited_once()

    def test_delete_git_failure_preserves_record(
        self, client: TestClient, mock_server: MagicMock, clone_git: MagicMock
    ) -> None:
        clone_git.delete_clone.return_value = SimpleNamespace(success=False, error="dirty")
        response = client.delete("/api/source-control/clones/clone-1")
        assert response.status_code == 409
        assert "dirty" in response.json()["detail"]["error"]
        mock_server.services.clone_storage.delete.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/source-control/clones/{clone_id}/sync
# ---------------------------------------------------------------------------


class TestSyncClone:
    def test_foreign_clone_returns_conflict_before_sync(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_storage = MagicMock()
        mock_storage.get.side_effect = MachineOwnershipMismatchError(
            resource_kind="clone",
            resource_id="10000000-0000-4000-8000-000000000002",
            owner_machine_id="20000000-0000-4000-8000-000000000002",
            current_machine_id="20000000-0000-4000-8000-000000000001",
        )
        mock_server.services.clone_storage = mock_storage

        response = client.post(
            "/api/source-control/clones/10000000-0000-4000-8000-000000000002/sync"
        )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "machine_ownership_mismatch"
        mock_storage.record_sync.assert_not_called()

    def test_sync_no_storage(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.clone_storage = None
        response = client.post("/api/source-control/clones/clone-1/sync")
        assert response.status_code == 503

    def test_sync_not_found(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.clone_storage = mock_storage

        response = client.post("/api/source-control/clones/clone-999/sync")
        assert response.status_code == 404

    def test_sync_success(
        self, client: TestClient, mock_server: MagicMock, clone_git: MagicMock
    ) -> None:
        response = client.post("/api/source-control/clones/clone-1/sync")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == "clone-1"
        mock_server.services.clone_storage.record_sync.assert_called_once_with("clone-1")
        clone_git.sync_clone.assert_awaited_once_with(
            clone_path=mock_server.services.clone_storage.get.return_value.clone_path,
            direction="pull",
        )

    def test_sync_git_failure_does_not_record_success(
        self, client: TestClient, mock_server: MagicMock, clone_git: MagicMock
    ) -> None:
        clone_git.sync_clone.return_value = SimpleNamespace(success=False, error="conflict")
        response = client.post("/api/source-control/clones/clone-1/sync")
        assert response.status_code == 409
        assert "conflict" in response.json()["detail"]["error"]
        mock_server.services.clone_storage.record_sync.assert_not_called()


def test_resolve_project_uses_machine_checkout(  # tdd-red window
    mock_server: MagicMock,
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    mock_server.session_manager.db = temp_db
    from gobby.servers.routes.source_control import _resolve_project

    repo_path, github_repo = _resolve_project(mock_server, isolated.project.id)

    assert repo_path == isolated.root_path
    assert github_repo == isolated.project.github_repo


@pytest.mark.parametrize(
    ("upstream", "track", "expected_ahead", "expected_behind"),
    [
        ("origin/feature/test", "[ahead 3, behind 2]", 3, 2),
        ("", "", None, None),
    ],
)
def test_status_reports_ahead_behind(
    client: TestClient,
    mock_server: MagicMock,
    upstream: str,
    track: str,
    expected_ahead: int | None,
    expected_behind: int | None,
) -> None:
    mock_server.services.worktree_storage = None
    mock_server.services.clone_storage = None

    async def run_git(args: list[str], _cwd: str, timeout: int = 10) -> SimpleNamespace:
        del timeout
        if args == ["branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="feature/test\n")
        if args == ["branch", "--list"]:
            return SimpleNamespace(returncode=0, stdout="  main\n* feature/test\n  develop\n")
        return SimpleNamespace(returncode=0, stdout=f"{upstream}\t{track}\n")

    with (
        patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=("/tmp/repo", "owner/repo"),
        ),
        patch(
            "gobby.servers.routes.source_control._run_git",
            new_callable=AsyncMock,
            side_effect=run_git,
        ) as mock_run_git,
    ):
        response = client.get(
            "/api/source-control/status", params={"project_id": f"project-{expected_ahead}"}
        )
        cached_response = client.get(
            "/api/source-control/status", params={"project_id": f"project-{expected_ahead}"}
        )

    expected = {
        "github_repo": "owner/repo",
        "current_branch": "feature/test",
        "branch_count": 3,
        "worktree_count": 0,
        "clone_count": 0,
        "repo_path": "/tmp/repo",
        "ahead": expected_ahead,
        "behind": expected_behind,
    }
    assert response.status_code == 200
    assert response.json() == expected
    assert cached_response.json() == expected
    assert mock_run_git.await_count == 3


def test_source_control_missing_checkout_is_409(  # tdd-red window
    client: TestClient,
    mock_server: MagicMock,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="sc-no-checkout")
    mock_server.session_manager.db = temp_db

    response = client.get("/api/source-control/branches", params={"project_id": project.id})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "CheckoutNotFoundError"


def test_create_client_worktree(
    client: TestClient,
    mock_server: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create client worktrees and translate the documented request errors."""
    monkeypatch.setenv("HOME", str(tmp_path))
    storage = MagicMock()
    git_manager = MagicMock()
    git_manager.repo_path = "/repo"
    git_manager.has_unpushed_commits = AsyncMock(return_value=(False, 0))
    git_manager.create_worktree = AsyncMock(return_value=SimpleNamespace(success=True, error=None))
    worktree = MagicMock(
        id="wt-client",
        project_id="project-1",
        branch_name="feature/client",
        worktree_path=str(tmp_path / ".gobby/worktrees/repo/feature-client"),
        base_branch="main",
        task_id=None,
    )
    worktree.to_dict.return_value = {
        "id": "wt-client",
        "project_id": "project-1",
        "branch_name": "feature/client",
        "worktree_path": worktree.worktree_path,
        "base_branch": "main",
        "task_id": None,
        "workspace_role": "client",
    }
    storage.get_by_branch.return_value = None
    storage.create.return_value = worktree
    mock_server.services.worktree_storage = storage

    def resolve_project(_server: MagicMock, project_id: str) -> tuple[str | None, None]:
        return (None, None) if project_id == "missing" else ("/repo", None)

    with (
        patch(
            "gobby.servers.routes.source_control_git._resolve_project",
            side_effect=resolve_project,
        ),
        patch("gobby.worktrees.git.WorktreeGitManager", return_value=git_manager),
        patch("gobby.utils.project_context.ensure_project_json_for_isolation"),
        patch(
            "gobby.worktrees.events.emit_worktree_event",
            return_value={"event_type": "worktree_created", "worktree_id": "wt-client"},
        ) as emit_event,
    ):
        response = client.post(
            "/api/source-control/worktrees",
            json={
                "project_id": "project-1",
                "branch_name": "feature/client",
                "workspace_role": "client",
            },
        )
        assert response.status_code == 200
        assert response.json() == worktree.to_dict.return_value

        existing = MagicMock(id="wt-existing", worktree_path="/tmp/existing")
        storage.get_by_branch.return_value = existing
        conflict = client.post(
            "/api/source-control/worktrees",
            json={
                "project_id": "project-1",
                "branch_name": "feature/conflict",
                "workspace_role": "client",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["error_code"] == "branch_conflict"

        missing = client.post(
            "/api/source-control/worktrees",
            json={
                "project_id": "missing",
                "branch_name": "feature/missing",
                "workspace_role": "client",
            },
        )
        assert missing.status_code == 404

        invalid = client.post(
            "/api/source-control/worktrees",
            json={
                "project_id": "project-1",
                "branch_name": "bad;branch",
                "workspace_role": "client",
            },
        )
        assert invalid.status_code == 400

    create_kwargs = storage.create.call_args.kwargs
    assert create_kwargs["workspace_role"] == "client"
    assert create_kwargs["task_id"] is None
    assert create_kwargs["worktree_path"] == worktree.worktree_path
    git_manager.create_worktree.assert_called_once_with(
        worktree_path=worktree.worktree_path,
        branch_name="feature/client",
        base_branch="main",
        create_branch=True,
        use_local=False,
    )
    emit_event.assert_called_once_with(
        "worktree_created",
        worktree_id="wt-client",
        project_id="project-1",
        branch_name="feature/client",
        worktree_path=worktree.worktree_path,
        base_branch="main",
        task_id=None,
    )


def test_delete_client_worktree_refuses_live_terminals(
    client: TestClient,
    mock_server: MagicMock,
) -> None:
    """A client worktree remains until every Gobby-owned terminal exits."""
    worktree = SimpleNamespace(
        id="wt-client",
        project_id="project-1",
        branch_name="feature/client",
        worktree_path="/tmp/client-worktree",
        base_branch="main",
        task_id=None,
        workspace_role="client",
    )
    terminal = SimpleNamespace(
        id="terminal-1",
        ownership="gobby",
        state="live",
        session_id="session-1",
    )
    storage = MagicMock()
    storage.get.return_value = worktree
    storage.delete.return_value = True
    terminal_manager = MagicMock()
    terminal_manager.list_by_project.return_value = [terminal]
    mock_server.services.worktree_storage = storage
    mock_server.services.terminal_manager = terminal_manager
    mock_server.session_manager.get.return_value = SimpleNamespace(
        workspace_path="/tmp/client-worktree/subdirectory"
    )

    refused = client.delete("/api/source-control/worktrees/wt-client")

    assert refused.status_code == 409
    assert refused.json()["detail"]["error_code"] == "terminals_live"
    storage.delete.assert_not_called()

    terminal.state = "exited"
    deleted = client.delete("/api/source-control/worktrees/wt-client")

    assert deleted.status_code == 200
    assert deleted.json()["success"] is True
    storage.delete.assert_called_once_with("wt-client")
