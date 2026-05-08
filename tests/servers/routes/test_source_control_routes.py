"""Tests for source control API routes.

Exercises src/gobby/servers/routes/source_control.py endpoints and helper
functions using create_http_server() with mocked services.
"""

from __future__ import annotations

import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

import gobby.servers.routes.source_control as sc_module
from gobby.servers.routes.source_control import (
    _get_cached,
    _parse_github_repo,
    _set_cached,
    _validate_git_ref,
    create_source_control_router,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the module-level cache before and after every test."""
    sc_module._cache.clear()
    yield
    sc_module._cache.clear()


@pytest.fixture
def mock_server():
    """Create a mock HTTPServer with minimal service plumbing."""
    server = MagicMock()
    server.session_manager = MagicMock()
    server.session_manager.db = MagicMock()
    server.services = MagicMock()
    server.services.mcp_manager = None
    server.services.worktree_storage = None
    server.services.clone_storage = None
    server.services.git_manager = None
    return server


@pytest.fixture
def client(mock_server):
    """Create a TestClient with the source control router mounted."""
    app = FastAPI()
    router = create_source_control_router(mock_server)
    app.include_router(router)
    return TestClient(app)


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
# Helper: _parse_github_repo
# ---------------------------------------------------------------------------


class TestParseGithubRepo:
    def test_valid_owner_repo(self) -> None:
        result = _parse_github_repo("octocat/hello-world")
        assert result == ("octocat", "hello-world")

    def test_none_input(self) -> None:
        assert _parse_github_repo(None) is None

    def test_empty_string(self) -> None:
        assert _parse_github_repo("") is None

    def test_no_slash(self) -> None:
        assert _parse_github_repo("just-a-name") is None

    def test_multiple_slashes(self) -> None:
        """Split on first slash only."""
        result = _parse_github_repo("org/repo/extra")
        assert result == ("org", "repo/extra")


# ---------------------------------------------------------------------------
# Helper: _resolve_project
# ---------------------------------------------------------------------------


class TestResolveProject:
    def test_resolve_with_project_id(self, mock_server) -> None:
        mock_project = MagicMock()
        mock_project.repo_path = "/tmp/repo"
        mock_project.github_repo = "owner/repo"

        mock_pm = MagicMock()
        mock_pm.get.return_value = mock_project

        with patch("gobby.servers.routes.source_control.LocalProjectManager", return_value=mock_pm):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, "proj-123")

        assert repo_path == "/tmp/repo"
        assert github_repo == "owner/repo"

    def test_resolve_without_project_id_falls_back(self, mock_server) -> None:
        mock_proj = MagicMock()
        mock_proj.name = "my-project"
        mock_proj.repo_path = "/tmp/fallback"
        mock_proj.github_repo = "org/fallback"

        mock_pm = MagicMock()
        mock_pm.list.return_value = [mock_proj]

        with patch("gobby.servers.routes.source_control.LocalProjectManager", return_value=mock_pm):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, None)

        assert repo_path == "/tmp/fallback"
        assert github_repo == "org/fallback"

    def test_resolve_skips_hidden_projects(self, mock_server) -> None:
        orphaned = MagicMock()
        orphaned.name = "_orphaned"
        orphaned.repo_path = "/tmp/orphaned"

        real = MagicMock()
        real.name = "real-project"
        real.repo_path = "/tmp/real"
        real.github_repo = None

        mock_pm = MagicMock()
        mock_pm.list.return_value = [orphaned, real]

        with patch("gobby.servers.routes.source_control.LocalProjectManager", return_value=mock_pm):
            from gobby.servers.routes.source_control import _resolve_project

            repo_path, github_repo = _resolve_project(mock_server, None)

        assert repo_path == "/tmp/real"
        assert github_repo is None

    def test_resolve_returns_none_none_on_failure(self, mock_server) -> None:
        mock_server.session_manager = None

        from gobby.servers.routes.source_control import _resolve_project

        repo_path, github_repo = _resolve_project(mock_server, "proj-123")
        assert repo_path is None
        assert github_repo is None


# ---------------------------------------------------------------------------
# Helper: _get_project_manager
# ---------------------------------------------------------------------------


class TestGetProjectManager:
    def test_raises_503_when_session_manager_is_none(self, mock_server) -> None:
        mock_server.session_manager = None

        from gobby.servers.routes.source_control import _get_project_manager

        with pytest.raises(HTTPException) as exc_info:
            _get_project_manager(mock_server)
        assert exc_info.value.status_code == 503

    def test_returns_project_manager(self, mock_server) -> None:
        with patch("gobby.servers.routes.source_control.LocalProjectManager") as mock_cls:
            from gobby.servers.routes.source_control import _get_project_manager

            _get_project_manager(mock_server)
            mock_cls.assert_called_once_with(mock_server.session_manager.db)
            assert mock_cls.call_count == 1
            assert mock_cls.call_args is not None


# ---------------------------------------------------------------------------
# Helper: _get_github / _call_github_mcp
# ---------------------------------------------------------------------------


class TestGetGithub:
    def test_returns_none_when_no_mcp_manager(self, mock_server) -> None:
        mock_server.services.mcp_manager = None

        from gobby.servers.routes.source_control import _get_github

        assert _get_github(mock_server) is None

    def test_returns_github_integration_when_mcp_available(self, mock_server) -> None:
        mock_server.services.mcp_manager = MagicMock()

        with patch("gobby.servers.routes.source_control.GitHubIntegration") as mock_cls:
            mock_cls.return_value = MagicMock()

            from gobby.servers.routes.source_control import _get_github

            result = _get_github(mock_server)
            assert result is not None


class TestCallGithubMcp:
    @pytest.mark.asyncio
    async def test_raises_503_when_no_mcp_manager(self, mock_server) -> None:
        mock_server.services.mcp_manager = None

        from gobby.servers.routes.source_control import _call_github_mcp

        with pytest.raises(HTTPException) as exc_info:
            await _call_github_mcp(mock_server, "some_tool", {})
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_parses_json_text_content(self, mock_server) -> None:
        mock_item = MagicMock()
        mock_item.text = '{"key": "value"}'

        mock_result = MagicMock()
        mock_result.content = [mock_item]

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = mock_result

        mock_server.services.mcp_manager = MagicMock()
        mock_server.services.mcp_manager.get_client_session = AsyncMock(return_value=mock_session)

        from gobby.servers.routes.source_control import _call_github_mcp

        result = await _call_github_mcp(mock_server, "test_tool", {"arg": "val"})
        assert result == {"key": "value"}
        assert mock_session.call_tool.await_args.args == ("test_tool", {"arg": "val"})

    @pytest.mark.asyncio
    async def test_returns_plain_text_on_json_decode_error(self, mock_server) -> None:
        mock_item = MagicMock()
        mock_item.text = "plain text response"

        mock_result = MagicMock()
        mock_result.content = [mock_item]

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = mock_result

        mock_server.services.mcp_manager = MagicMock()
        mock_server.services.mcp_manager.get_client_session = AsyncMock(return_value=mock_session)

        from gobby.servers.routes.source_control import _call_github_mcp

        result = await _call_github_mcp(mock_server, "test_tool", {})
        assert result == "plain text response"
        assert mock_session.call_tool.await_args.args == ("test_tool", {})

    @pytest.mark.asyncio
    async def test_raises_502_on_exception(self, mock_server) -> None:
        mock_server.services.mcp_manager = MagicMock()
        mock_server.services.mcp_manager.get_client_session = AsyncMock(
            side_effect=RuntimeError("Connection failed")
        )

        from gobby.servers.routes.source_control import _call_github_mcp

        with pytest.raises(HTTPException) as exc_info:
            await _call_github_mcp(mock_server, "test_tool", {})
        assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# GET /api/source-control/status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_status_no_repo_path(self, client, mock_server) -> None:
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
        assert data["github_available"] is False

    def test_status_with_repo_path(self, client, mock_server) -> None:
        """When repo_path resolves, runs git commands to get branch info."""
        mock_server.services.worktree_storage = None
        mock_server.services.clone_storage = None

        # Mock git responses
        branch_result = MagicMock(returncode=0, stdout="feature/test\n")
        list_result = MagicMock(returncode=0, stdout="  main\n* feature/test\n  develop\n")

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._run_git",
                new_callable=AsyncMock,
                side_effect=[branch_result, list_result],
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=None,
            ),
        ):
            response = client.get("/api/source-control/status")

        assert response.status_code == 200
        data = response.json()
        assert data["current_branch"] == "feature/test"
        assert data["branch_count"] == 3

    def test_status_with_worktree_and_clone_counts(self, client, mock_server) -> None:
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
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=None,
            ),
        ):
            response = client.get("/api/source-control/status")

        assert response.status_code == 200
        data = response.json()
        assert data["worktree_count"] == 2
        assert data["clone_count"] == 1

    def test_status_github_available(self, client, mock_server) -> None:
        mock_gh = MagicMock()
        mock_gh.is_available.return_value = True

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=mock_gh,
            ),
        ):
            response = client.get("/api/source-control/status")

        assert response.status_code == 200
        assert response.json()["github_available"] is True


# ---------------------------------------------------------------------------
# GET /api/source-control/branches
# ---------------------------------------------------------------------------


class TestListBranches:
    def test_branches_no_repo(self, client, mock_server) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/branches")

        assert response.status_code == 200
        data = response.json()
        assert data["branches"] == []
        assert data["current_branch"] is None

    def test_branches_with_data(self, client, mock_server) -> None:
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

    def test_branches_cached(self, client, mock_server) -> None:
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

    def test_branches_skips_remote_head(self, client, mock_server) -> None:
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

    def test_branches_skips_duplicate_remote(self, client, mock_server) -> None:
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
    def test_checkout_success_switches_existing_local_branch(self, client, mock_server) -> None:
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

    def test_checkout_invalid_ref_returns_400(self, client) -> None:
        response = client.post(
            "/api/source-control/branches/checkout",
            json={"branch_name": "..evil"},
        )

        assert response.status_code == 400

    def test_checkout_no_repo_returns_400(self, client) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.post(
                "/api/source-control/branches/checkout",
                json={"branch_name": "main"},
            )

        assert response.status_code == 400

    def test_checkout_missing_local_branch_returns_404(self, client) -> None:
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

    def test_checkout_failed_switch_returns_409(self, client) -> None:
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

    def test_checkout_success_invalidates_branch_cache(self, client) -> None:
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
    def test_commits_valid_branch(self, client, mock_server) -> None:
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

    def test_commits_no_repo(self, client, mock_server) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/branches/main/commits")

        assert response.status_code == 200
        assert response.json()["commits"] == []

    def test_commits_invalid_branch_name(self, client, mock_server) -> None:
        """Branch names with shell metacharacters are rejected."""
        response = client.get("/api/source-control/branches/;rm -rf/commits")
        assert response.status_code == 400

    def test_commits_branch_with_slashes(self, client, mock_server) -> None:
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

    def test_commits_with_limit(self, client, mock_server) -> None:
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


# ---------------------------------------------------------------------------
# GET /api/source-control/diff
# ---------------------------------------------------------------------------


class TestGetDiff:
    def test_diff_with_valid_refs(self, client, mock_server) -> None:
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

    def test_diff_no_repo_path(self, client, mock_server) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/diff")

        assert response.status_code == 400

    def test_diff_invalid_base_ref(self, client, mock_server) -> None:
        response = client.get("/api/source-control/diff?base=;evil&head=HEAD")
        assert response.status_code == 400

    def test_diff_invalid_head_ref(self, client, mock_server) -> None:
        response = client.get("/api/source-control/diff?base=main&head=..evil")
        assert response.status_code == 400

    def test_diff_timeout(self, client, mock_server) -> None:
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

    def test_diff_general_error(self, client, mock_server) -> None:
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

    def test_diff_patch_truncated(self, client, mock_server) -> None:
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
# GET /api/source-control/prs
# ---------------------------------------------------------------------------


class TestListPRs:
    def test_prs_no_github(self, client, mock_server) -> None:
        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, None),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=None,
            ),
        ):
            response = client.get("/api/source-control/prs")

        assert response.status_code == 200
        data = response.json()
        assert data["prs"] == []
        assert data["github_available"] is False

    def test_prs_github_available_no_repo(self, client, mock_server) -> None:
        mock_gh = MagicMock()
        mock_gh.is_available.return_value = True

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, None),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=mock_gh,
            ),
        ):
            response = client.get("/api/source-control/prs")

        assert response.status_code == 200
        data = response.json()
        assert data["github_available"] is True
        assert "error" in data

    def test_prs_with_data(self, client, mock_server) -> None:
        mock_gh = MagicMock()
        mock_gh.is_available.return_value = True

        pr_data = [
            {
                "number": 42,
                "title": "Add feature",
                "state": "open",
                "user": {"login": "alice"},
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
                "created_at": "2025-01-01",
                "updated_at": "2025-01-02",
                "draft": False,
            }
        ]

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=mock_gh,
            ),
            patch(
                "gobby.servers.routes.source_control._call_github_mcp",
                new_callable=AsyncMock,
                return_value=pr_data,
            ),
        ):
            response = client.get("/api/source-control/prs")

        assert response.status_code == 200
        data = response.json()
        assert data["github_available"] is True
        assert len(data["prs"]) == 1
        pr = data["prs"][0]
        assert pr["number"] == 42
        assert pr["title"] == "Add feature"
        assert pr["author"] == "alice"
        assert pr["head_branch"] == "feature"

    def test_prs_github_not_available(self, client, mock_server) -> None:
        mock_gh = MagicMock()
        mock_gh.is_available.return_value = False

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=mock_gh,
            ),
        ):
            response = client.get("/api/source-control/prs")

        assert response.status_code == 200
        assert response.json()["github_available"] is False

    def test_prs_cached(self, client, mock_server) -> None:
        """Second call returns cached result."""
        mock_gh = MagicMock()
        mock_gh.is_available.return_value = True

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=mock_gh,
            ),
            patch(
                "gobby.servers.routes.source_control._call_github_mcp",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_mcp,
        ):
            first_response = client.get("/api/source-control/prs")
            second_response = client.get("/api/source-control/prs")
            # MCP should only be called once due to caching
            assert mock_mcp.call_count == 1
            assert first_response.status_code == 200
            assert second_response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/source-control/prs/{number}
# ---------------------------------------------------------------------------


class TestGetPR:
    def test_get_pr_no_github_repo(self, client, mock_server) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/prs/42")

        assert response.status_code == 400

    def test_get_pr_success(self, client, mock_server) -> None:
        pr_detail = {"number": 42, "title": "My PR", "body": "Details"}

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._call_github_mcp",
                new_callable=AsyncMock,
                return_value=pr_detail,
            ),
        ):
            response = client.get("/api/source-control/prs/42")

        assert response.status_code == 200
        data = response.json()
        assert data["pr"]["number"] == 42
        assert data["github_available"] is True


# ---------------------------------------------------------------------------
# GET /api/source-control/prs/{number}/checks
# ---------------------------------------------------------------------------


class TestGetPRChecks:
    def test_checks_no_github_repo(self, client, mock_server) -> None:
        with patch(
            "gobby.servers.routes.source_control._resolve_project",
            return_value=(None, None),
        ):
            response = client.get("/api/source-control/prs/42/checks")

        assert response.status_code == 400

    def test_checks_success(self, client, mock_server) -> None:
        pr_data = {"head": {"sha": "abc123"}}
        checks_data = [{"name": "CI", "status": "completed", "conclusion": "success"}]

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._call_github_mcp",
                new_callable=AsyncMock,
                side_effect=[pr_data, checks_data],
            ),
        ):
            response = client.get("/api/source-control/prs/42/checks")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert len(data["checks"]) == 1

    def test_checks_no_head_sha(self, client, mock_server) -> None:
        pr_data = {"head": {}}

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._call_github_mcp",
                new_callable=AsyncMock,
                return_value=pr_data,
            ),
        ):
            response = client.get("/api/source-control/prs/42/checks")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unknown"
        assert data["checks"] == []

    def test_checks_error(self, client, mock_server) -> None:
        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._call_github_mcp",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API down"),
            ),
        ):
            response = client.get("/api/source-control/prs/42/checks")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "API down" in data["error"]


# ---------------------------------------------------------------------------
# GET /api/source-control/cicd/runs
# ---------------------------------------------------------------------------


class TestListCICDRuns:
    def test_cicd_no_github(self, client, mock_server) -> None:
        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, None),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=None,
            ),
        ):
            response = client.get("/api/source-control/cicd/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["runs"] == []
        assert data["github_available"] is False

    def test_cicd_with_runs(self, client, mock_server) -> None:
        mock_gh = MagicMock()
        mock_gh.is_available.return_value = True

        workflow_data = {
            "workflow_runs": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "event": "push",
                    "created_at": "2025-01-01",
                    "html_url": "https://github.com/owner/repo/actions/runs/1",
                }
            ]
        }

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", "owner/repo"),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=mock_gh,
            ),
            patch(
                "gobby.servers.routes.source_control._call_github_mcp",
                new_callable=AsyncMock,
                return_value=workflow_data,
            ),
        ):
            response = client.get("/api/source-control/cicd/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["github_available"] is True
        assert len(data["runs"]) == 1
        assert data["runs"][0]["name"] == "CI"
        assert data["runs"][0]["conclusion"] == "success"

    def test_cicd_no_repo_configured(self, client, mock_server) -> None:
        mock_gh = MagicMock()
        mock_gh.is_available.return_value = True

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=(None, None),
            ),
            patch(
                "gobby.servers.routes.source_control._get_github",
                return_value=mock_gh,
            ),
        ):
            response = client.get("/api/source-control/cicd/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["github_available"] is True
        assert "error" in data


# ---------------------------------------------------------------------------
# GET /api/source-control/worktrees
# ---------------------------------------------------------------------------


class TestListWorktrees:
    def test_worktrees_no_storage(self, client, mock_server) -> None:
        mock_server.services.worktree_storage = None
        response = client.get("/api/source-control/worktrees")
        assert response.status_code == 200
        assert response.json()["worktrees"] == []

    def test_worktrees_with_storage(self, client, mock_server) -> None:
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

    def test_worktrees_with_project_id_filter(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_storage.list_worktrees.return_value = []
        mock_server.services.worktree_storage = mock_storage

        response = client.get("/api/source-control/worktrees?project_id=proj-1")
        assert response.status_code == 200
        mock_storage.list_worktrees.assert_called_once_with(project_id="proj-1", status=None)

    def test_worktrees_with_status_filter(self, client, mock_server) -> None:
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
    def test_stats_no_storage(self, client, mock_server) -> None:
        mock_server.services.worktree_storage = None
        response = client.get("/api/source-control/worktrees/stats")
        assert response.status_code == 200
        assert response.json()["stats"] == {}

    def test_stats_no_project_id(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_server.services.worktree_storage = mock_storage

        response = client.get("/api/source-control/worktrees/stats")
        assert response.status_code == 200
        assert response.json()["stats"] == {}

    def test_stats_with_project_id(self, client, mock_server) -> None:
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
    def test_delete_no_storage(self, client, mock_server) -> None:
        mock_server.services.worktree_storage = None
        response = client.delete("/api/source-control/worktrees/wt-1")
        assert response.status_code == 503

    def test_delete_not_found(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.worktree_storage = mock_storage

        response = client.delete("/api/source-control/worktrees/wt-999")
        assert response.status_code == 404

    def test_delete_success_no_git_manager(self, client, mock_server) -> None:
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

    def test_delete_success_with_git_manager(self, client, mock_server) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.project_id = "proj-1"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_storage.delete.return_value = True
        mock_server.services.worktree_storage = mock_storage

        mock_git_result = MagicMock()
        mock_git_result.success = True
        mock_git_manager = MagicMock()
        mock_server.services.git_manager = mock_git_manager

        with (
            patch(
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.worktrees.git.WorktreeGitManager",
            ) as mock_wgm_cls,
        ):
            mock_wgm_cls.return_value.delete_worktree.return_value = mock_git_result

            response = client.delete("/api/source-control/worktrees/wt-1")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["git_deleted"] is True

    def test_delete_git_deletion_fails(self, client, mock_server) -> None:
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
                "gobby.servers.routes.source_control._resolve_project",
                return_value=("/tmp/repo", None),
            ),
            patch(
                "gobby.worktrees.git.WorktreeGitManager",
            ) as mock_wgm_cls,
        ):
            mock_wgm_cls.return_value.delete_worktree.return_value = mock_git_result

            response = client.delete("/api/source-control/worktrees/wt-1")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True  # DB record still deleted
        assert data["git_deleted"] is False
        assert "message" in data


# ---------------------------------------------------------------------------
# POST /api/source-control/worktrees/cleanup
# ---------------------------------------------------------------------------


class TestCleanupWorktrees:
    def test_cleanup_no_storage(self, client, mock_server) -> None:
        mock_server.services.worktree_storage = None
        response = client.post("/api/source-control/worktrees/cleanup")
        assert response.status_code == 200
        assert response.json()["candidates"] == []

    def test_cleanup_no_project_id(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_server.services.worktree_storage = mock_storage

        response = client.post("/api/source-control/worktrees/cleanup")
        assert response.status_code == 200
        assert response.json()["candidates"] == []

    def test_cleanup_dry_run(self, client, mock_server) -> None:
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

    def test_cleanup_execute(self, client, mock_server) -> None:
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
    def test_sync_no_storage(self, client, mock_server) -> None:
        mock_server.services.worktree_storage = None
        response = client.post("/api/source-control/worktrees/wt-1/sync")
        assert response.status_code == 503

    def test_sync_not_found(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.worktree_storage = mock_storage

        response = client.post("/api/source-control/worktrees/wt-999/sync")
        assert response.status_code == 404

    def test_sync_with_git_manager(self, client, mock_server) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.base_branch = "main"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_server.services.worktree_storage = mock_storage

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.message = "Synced successfully"
        mock_git = MagicMock()
        mock_git.sync_from_main.return_value = mock_result
        mock_server.services.git_manager = mock_git

        response = client.post("/api/source-control/worktrees/wt-1/sync")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "Synced successfully"
        assert data["id"] == "wt-1"

    def test_sync_without_git_manager_falls_back(self, client, mock_server) -> None:
        wt = MagicMock()
        wt.worktree_path = "/tmp/wt"
        wt.base_branch = "main"

        mock_storage = MagicMock()
        mock_storage.get.return_value = wt
        mock_storage.sync.return_value = {"success": True, "id": "wt-1"}
        mock_server.services.worktree_storage = mock_storage
        mock_server.services.git_manager = None

        response = client.post("/api/source-control/worktrees/wt-1/sync")
        assert response.status_code == 200
        mock_storage.sync.assert_called_once_with("wt-1")


# ---------------------------------------------------------------------------
# GET /api/source-control/clones
# ---------------------------------------------------------------------------


class TestListClones:
    def test_clones_no_storage(self, client, mock_server) -> None:
        mock_server.services.clone_storage = None
        response = client.get("/api/source-control/clones")
        assert response.status_code == 200
        assert response.json()["clones"] == []

    def test_clones_with_storage(self, client, mock_server) -> None:
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

    def test_clones_with_project_id(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_storage.list_clones.return_value = []
        mock_server.services.clone_storage = mock_storage

        response = client.get("/api/source-control/clones?project_id=proj-1")
        assert response.status_code == 200
        mock_storage.list_clones.assert_called_once_with(project_id="proj-1")


# ---------------------------------------------------------------------------
# DELETE /api/source-control/clones/{clone_id}
# ---------------------------------------------------------------------------


class TestDeleteClone:
    def test_delete_no_storage(self, client, mock_server) -> None:
        mock_server.services.clone_storage = None
        response = client.delete("/api/source-control/clones/clone-1")
        assert response.status_code == 503

    def test_delete_not_found(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.clone_storage = mock_storage

        response = client.delete("/api/source-control/clones/clone-999")
        assert response.status_code == 404

    def test_delete_success(self, client, mock_server) -> None:
        clone = MagicMock()
        mock_storage = MagicMock()
        mock_storage.get.return_value = clone
        mock_storage.delete.return_value = True
        mock_server.services.clone_storage = mock_storage

        response = client.delete("/api/source-control/clones/clone-1")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == "clone-1"

    def test_delete_returns_false(self, client, mock_server) -> None:
        """When storage.delete returns False (e.g. DB constraint), success=False."""
        clone = MagicMock()
        mock_storage = MagicMock()
        mock_storage.get.return_value = clone
        mock_storage.delete.return_value = False
        mock_server.services.clone_storage = mock_storage

        response = client.delete("/api/source-control/clones/clone-1")
        assert response.status_code == 200
        assert response.json()["success"] is False


# ---------------------------------------------------------------------------
# POST /api/source-control/clones/{clone_id}/sync
# ---------------------------------------------------------------------------


class TestSyncClone:
    def test_sync_no_storage(self, client, mock_server) -> None:
        mock_server.services.clone_storage = None
        response = client.post("/api/source-control/clones/clone-1/sync")
        assert response.status_code == 503

    def test_sync_not_found(self, client, mock_server) -> None:
        mock_storage = MagicMock()
        mock_storage.get.return_value = None
        mock_server.services.clone_storage = mock_storage

        response = client.post("/api/source-control/clones/clone-999/sync")
        assert response.status_code == 404

    def test_sync_success(self, client, mock_server) -> None:
        clone = MagicMock()
        mock_storage = MagicMock()
        mock_storage.get.return_value = clone
        mock_server.services.clone_storage = mock_storage

        response = client.post("/api/source-control/clones/clone-1/sync")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == "clone-1"
        mock_storage.record_sync.assert_called_once_with("clone-1")
