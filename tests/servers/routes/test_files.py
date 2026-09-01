import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from gobby.servers.routes.files import (
    MAX_READ_SIZE,
    _get_git_tracked_files,
    create_files_router,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.unit

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
HIDDEN_PROJECT_ID = "00000000-0000-0000-0000-000000000002"
GIT_PROJECT_ID = "22222222-2222-4222-8222-222222222222"
GIT_DIFF_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
UNKNOWN_PROJECT_ID = "99999999-9999-4999-8999-999999999999"


@pytest.mark.asyncio
async def test_get_git_tracked_files_expected_failure_returns_none() -> None:
    with patch(
        "gobby.servers.routes.files._run_git",
        new=AsyncMock(side_effect=OSError("git unavailable")),
    ):
        result = await _get_git_tracked_files("/project")

    assert result is None


@pytest.mark.asyncio
async def test_get_git_tracked_files_unexpected_error_propagates() -> None:
    with (
        patch(
            "gobby.servers.routes.files._run_git",
            new=AsyncMock(side_effect=RuntimeError("programmer error")),
        ),
        pytest.raises(RuntimeError, match="programmer error"),
    ):
        await _get_git_tracked_files("/project")


class TestFilesRoutes:
    @pytest.fixture
    def project_dir(self, tmp_path: Path) -> Path:
        """Create a temporary project directory with test files."""
        # Create directory structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')\n")
        (tmp_path / "src" / "utils.py").write_text("def add(a, b): return a + b\n")
        (tmp_path / "README.md").write_text("# Test Project\n")
        (tmp_path / "config.json").write_text('{"key": "value"}\n')
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n")
        (tmp_path / "images").mkdir()
        # Create a tiny PNG (1x1 pixel)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
            b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
            b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        (tmp_path / "images" / "logo.png").write_bytes(png_bytes)
        return tmp_path

    @pytest.fixture
    def mock_project(self, project_dir: Path) -> MagicMock:
        project = MagicMock()
        project.id = PROJECT_ID
        project.name = "test-project"
        project.repo_path = str(project_dir)
        return project

    @pytest.fixture
    def mock_server(self, mock_project: MagicMock, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        server = MagicMock()
        db = MagicMock()
        server.session_manager = MagicMock()
        server.session_manager.db = db

        # Mock fetchall for project listing
        db.fetchall.return_value = [
            {
                "id": mock_project.id,
                "name": mock_project.name,
                "repo_path": mock_project.repo_path,
                "github_url": None,
                "github_repo": None,
                "linear_team_id": None,
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ]

        # Mock fetchone for project get
        def mock_fetchone(query: str, params: tuple) -> dict | None:
            if params and params[0] == mock_project.id:
                return {
                    "id": mock_project.id,
                    "name": mock_project.name,
                    "repo_path": mock_project.repo_path,
                    "github_url": None,
                    "github_repo": None,
                    "linear_team_id": None,
                    "created_at": "2024-01-01T00:00:00",
                    "updated_at": "2024-01-01T00:00:00",
                }
            return None

        db.fetchone.side_effect = mock_fetchone

        async def run_db(func, *args, **kwargs):
            return func(*args, **kwargs)

        server.run_db = AsyncMock(side_effect=run_db)

        def checkout_root(_server: MagicMock, project_id: str) -> str:
            row = server.session_manager.db.fetchone("", (project_id,))
            if not row:
                raise HTTPException(404, f"Project not found: {project_id}")
            return str(row["repo_path"])

        monkeypatch.setattr(
            "gobby.servers.routes.files._require_project_checkout_root",
            checkout_root,
        )

        def projects_to_responses(_server: MagicMock, projects: list[Any]) -> list[dict[str, Any]]:
            return [
                {
                    "id": project.id,
                    "name": project.name,
                    "checkout": {
                        "machine_id": "test-machine",
                        "root_path": mock_project.repo_path,
                    },
                }
                for project in projects
            ]

        monkeypatch.setattr(
            "gobby.servers.routes.files._projects_to_responses",
            projects_to_responses,
        )
        return server

    @pytest.fixture
    def client(self, mock_server: MagicMock) -> TestClient:
        app = FastAPI()
        router = create_files_router(mock_server)
        app.include_router(router)
        return TestClient(app)

    # -- /projects --

    def test_list_projects(self, client: TestClient) -> None:
        resp = client.get("/api/files/projects")
        assert resp.status_code == 200
        data = resp.json()
        row = next(project for project in data if project["id"] == PROJECT_ID)
        assert row["name"] == "test-project"
        assert "repo_path" not in row
        assert row["checkout"]["root_path"]

    def test_list_projects_hides_system_projects(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        rows = list(mock_server.session_manager.db.fetchall.return_value)
        hidden = dict(rows[0], id=HIDDEN_PROJECT_ID, name="_global")
        mock_server.session_manager.db.fetchall.return_value = [hidden, *rows]

        resp = client.get("/api/files/projects")

        assert resp.status_code == 200
        names = [project["name"] for project in resp.json()]
        assert "_global" not in names
        assert "test-project" in names

    # -- /tree --

    def test_tree_root(self, client: TestClient, project_dir: Path) -> None:
        resp = client.get("/api/files/tree", params={"project_id": PROJECT_ID, "path": ""})
        assert resp.status_code == 200
        entries = resp.json()
        names = [e["name"] for e in entries]
        # .git should be filtered out
        assert ".git" not in names
        # Directories should come first
        dir_entries = [e for e in entries if e["is_dir"]]
        file_entries = [e for e in entries if not e["is_dir"]]
        # Check ordering: dirs before files
        if dir_entries and file_entries:
            dir_indices = [entries.index(e) for e in dir_entries]
            file_indices = [entries.index(e) for e in file_entries]
            assert max(dir_indices) < min(file_indices)

    def test_tree_subdirectory(self, client: TestClient) -> None:
        resp = client.get("/api/files/tree", params={"project_id": PROJECT_ID, "path": "src"})
        assert resp.status_code == 200
        entries = resp.json()
        names = [e["name"] for e in entries]
        assert "main.py" in names
        assert "utils.py" in names

    def test_tree_nonexistent_project(self, client: TestClient) -> None:
        resp = client.get("/api/files/tree", params={"project_id": UNKNOWN_PROJECT_ID, "path": ""})
        assert resp.status_code == 404

    def test_tree_not_a_directory(self, client: TestClient) -> None:
        resp = client.get("/api/files/tree", params={"project_id": PROJECT_ID, "path": "README.md"})
        assert resp.status_code == 400

    # -- /read --

    def test_read_file(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/read", params={"project_id": PROJECT_ID, "path": "src/main.py"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "print('hello')\n"
        assert data["binary"] is False
        assert data["image"] is False
        assert data["truncated"] is False
        assert data["size"] > 0

    def test_read_json_file(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/read", params={"project_id": PROJECT_ID, "path": "config.json"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert '"key"' in data["content"]

    def test_read_image_returns_metadata_only(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/read", params={"project_id": PROJECT_ID, "path": "images/logo.png"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["image"] is True
        assert data["binary"] is True
        assert data["content"] is None

    def test_read_nonexistent_file(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/read", params={"project_id": PROJECT_ID, "path": "nonexistent.txt"}
        )
        assert resp.status_code == 404

    def test_read_truncation(
        self,
        client: TestClient,
        project_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Create a file larger than max_size
        large_content = "x" * 1000
        large_path = project_dir / "large.txt"
        large_path.write_text(large_content)

        bounded_reader = MagicMock()
        bounded_reader.__enter__.return_value = bounded_reader
        bounded_reader.read.side_effect = lambda byte_limit: large_content.encode()[:byte_limit]
        original_open = Path.open

        def instrumented_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == large_path:
                return bounded_reader
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", instrumented_open)

        resp = client.get(
            "/api/files/read",
            params={"project_id": PROJECT_ID, "path": "large.txt", "max_size": 100},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        assert len(data["content"]) == 100
        bounded_reader.read.assert_called_once_with(101)

    @pytest.mark.parametrize("max_size", [-1, MAX_READ_SIZE + 1])
    def test_read_rejects_out_of_range_max_size(
        self,
        client: TestClient,
        max_size: int,
    ) -> None:
        resp = client.get(
            "/api/files/read",
            params={"project_id": PROJECT_ID, "path": "README.md", "max_size": max_size},
        )
        assert resp.status_code == 422

    # -- /image --

    def test_serve_image(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/image", params={"project_id": PROJECT_ID, "path": "images/logo.png"}
        )
        assert resp.status_code == 200
        assert "image" in resp.headers["content-type"]

    def test_serve_svg_uses_restrictive_content_security_policy(
        self, client: TestClient, project_dir: Path
    ) -> None:
        (project_dir / "images" / "active.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        )

        resp = client.get(
            "/api/files/image", params={"project_id": PROJECT_ID, "path": "images/active.svg"}
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert resp.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_serve_non_image_rejected(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/image", params={"project_id": PROJECT_ID, "path": "README.md"}
        )
        assert resp.status_code == 400

    # -- Path traversal --

    def test_path_traversal_blocked(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/read",
            params={"project_id": PROJECT_ID, "path": "../../etc/passwd"},
        )
        assert resp.status_code == 403

    def test_path_traversal_in_tree(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/tree",
            params={"project_id": PROJECT_ID, "path": "../"},
        )
        assert resp.status_code == 403

    def test_path_traversal_in_image(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/image",
            params={"project_id": PROJECT_ID, "path": "../../etc/passwd"},
        )
        assert resp.status_code == 403

    # -- /write --

    def test_write_file(self, client: TestClient, project_dir: Path) -> None:
        resp = client.post(
            "/api/files/write",
            json={
                "project_id": PROJECT_ID,
                "path": "src/main.py",
                "content": "print('updated')\n",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # Verify file was actually written
        assert (project_dir / "src" / "main.py").read_text() == "print('updated')\n"

    def test_write_new_file(self, client: TestClient, project_dir: Path) -> None:
        resp = client.post(
            "/api/files/write",
            json={"project_id": PROJECT_ID, "path": "src/new_file.py", "content": "# new\n"},
        )
        assert resp.status_code == 200
        assert (project_dir / "src" / "new_file.py").read_text() == "# new\n"

    def test_write_to_git_dir_blocked(self, client: TestClient) -> None:
        resp = client.post(
            "/api/files/write",
            json={"project_id": PROJECT_ID, "path": ".git/config", "content": "hacked"},
        )
        assert resp.status_code == 403

    def test_write_path_traversal_blocked(self, client: TestClient) -> None:
        resp = client.post(
            "/api/files/write",
            json={"project_id": PROJECT_ID, "path": "../../etc/evil", "content": "bad"},
        )
        assert resp.status_code == 403

    def test_write_nonexistent_parent(self, client: TestClient) -> None:
        resp = client.post(
            "/api/files/write",
            json={
                "project_id": PROJECT_ID,
                "path": "nonexistent/dir/file.txt",
                "content": "x",
            },
        )
        assert resp.status_code == 404

    # -- /git-status --

    def test_git_status(self, client: TestClient, tmp_path: Path, mock_server: MagicMock) -> None:
        # Use a fresh directory for git tests (no pre-created .git)
        import subprocess

        git_dir = tmp_path / "git_project"
        git_dir.mkdir()
        (git_dir / "README.md").write_text("# Test\n")
        (git_dir / "main.py").write_text("print('hi')\n")

        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }
        subprocess.run(["git", "init"], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=git_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init", "--no-gpg-sign"],
            cwd=git_dir,
            capture_output=True,
            env=git_env,
        )

        # Modify a file to create a dirty state
        (git_dir / "README.md").write_text("# Modified\n")

        # Point mock to the git project
        mock_server.session_manager.db.fetchone.side_effect = (
            lambda q, p: {
                "id": GIT_PROJECT_ID,
                "name": "git-proj",
                "repo_path": str(git_dir),
                "github_url": None,
                "github_repo": None,
                "linear_team_id": None,
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
            if p and p[0] == GIT_PROJECT_ID
            else None
        )

        resp = client.get("/api/files/git-status", params={"project_id": GIT_PROJECT_ID})
        assert resp.status_code == 200
        data = resp.json()
        assert data["branch"] is not None
        assert isinstance(data["files"], dict)
        assert "README.md" in data["files"]

    def test_git_status_nonexistent_project(self, client: TestClient) -> None:
        resp = client.get("/api/files/git-status", params={"project_id": UNKNOWN_PROJECT_ID})
        assert resp.status_code == 404

    def test_git_status_expected_process_failure_returns_empty_status(
        self, client: TestClient
    ) -> None:
        with patch(
            "gobby.servers.routes.files._run_git",
            new=AsyncMock(side_effect=OSError("git unavailable")),
        ):
            resp = client.get("/api/files/git-status", params={"project_id": PROJECT_ID})

        assert resp.status_code == 200
        assert resp.json() == {"branch": None, "files": {}}

    def test_git_status_unexpected_error_propagates(self, client: TestClient) -> None:
        with (
            patch(
                "gobby.servers.routes.files._run_git",
                new=AsyncMock(side_effect=RuntimeError("programmer error")),
            ),
            pytest.raises(RuntimeError, match="programmer error"),
        ):
            client.get("/api/files/git-status", params={"project_id": PROJECT_ID})

    # -- /git-diff --

    def test_git_diff(self, client: TestClient, tmp_path: Path, mock_server: MagicMock) -> None:
        import subprocess

        git_dir = tmp_path / "git_diff_project"
        git_dir.mkdir()
        (git_dir / "README.md").write_text("# Test\n")

        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }
        subprocess.run(["git", "init"], cwd=git_dir, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=git_dir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init", "--no-gpg-sign"],
            cwd=git_dir,
            capture_output=True,
            env=git_env,
            check=True,
        )
        (git_dir / "README.md").write_text("# Modified\n")

        mock_server.session_manager.db.fetchone.side_effect = (
            lambda q, p: {
                "id": GIT_DIFF_PROJECT_ID,
                "name": "git-diff-proj",
                "repo_path": str(git_dir),
                "github_url": None,
                "github_repo": None,
                "linear_team_id": None,
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
            if p and p[0] == GIT_DIFF_PROJECT_ID
            else None
        )

        resp = client.get(
            "/api/files/git-diff",
            params={"project_id": GIT_DIFF_PROJECT_ID, "path": "README.md"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "diff" in data
        assert "Modified" in data["diff"]

    def test_git_diff_path_traversal(self, client: TestClient) -> None:
        resp = client.get(
            "/api/files/git-diff",
            params={"project_id": PROJECT_ID, "path": "../../etc/passwd"},
        )
        assert resp.status_code == 403

    # -- Session manager not available --

    def test_no_session_manager(self) -> None:
        server = MagicMock()
        server.session_manager = None
        app = FastAPI()
        router = create_files_router(server)
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/files/projects")
        assert resp.status_code == 503


def _files_client_for_db(db: HubDatabase) -> TestClient:
    server = MagicMock()
    server.session_manager = MagicMock()
    server.session_manager.db = db

    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    server.run_db = AsyncMock(side_effect=run_db)
    app = FastAPI()
    app.include_router(create_files_router(server))
    return TestClient(app)


def test_list_directory_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    (tmp_path / "repo" / "hello.txt").write_text("hi\n", encoding="utf-8")
    client = _files_client_for_db(temp_db)

    resp = client.get("/api/files/tree", params={"project_id": isolated.project.id, "path": ""})

    assert resp.status_code == 200
    assert "hello.txt" in [entry["name"] for entry in resp.json()]


def test_list_directory_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="files-no-checkout")
    client = _files_client_for_db(temp_db)

    resp = client.get("/api/files/tree", params={"project_id": project.id, "path": ""})

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "CheckoutNotFoundError"


def test_list_directory_overlay_without_primary_is_not_a_file_root(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = tmp_path / "wt"
    overlay.mkdir()
    (overlay / "only-overlay.txt").write_text("x\n", encoding="utf-8")
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="files-overlay-only")
    insert_overlay(
        temp_db,
        project_id=project.id,
        machine_id=machine_id,
        path=str(overlay),
        kind="worktree",
    )
    client = _files_client_for_db(temp_db)

    resp = client.get("/api/files/tree", params={"project_id": project.id, "path": ""})

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "CheckoutNotFoundError"


def test_list_projects_returns_checkout_shaped_json(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    monkeypatch.setattr(
        "gobby.servers.routes.projects.get_machine_id",
        lambda: isolated.machine_id,
    )
    client = _files_client_for_db(temp_db)

    resp = client.get("/api/files/projects")

    assert resp.status_code == 200
    row = next(project for project in resp.json() if project["id"] == isolated.project.id)
    assert "repo_path" not in row
    assert row["checkout"] == {
        "machine_id": isolated.machine_id,
        "root_path": isolated.root_path,
    }


def test_list_projects_checkout_null_without_primary(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    monkeypatch.setattr("gobby.servers.routes.projects.get_machine_id", lambda: machine_id)
    project = LocalProjectManager(temp_db).create(name="files-list-no-checkout")
    client = _files_client_for_db(temp_db)

    resp = client.get("/api/files/projects")

    assert resp.status_code == 200
    row = next(item for item in resp.json() if item["id"] == project.id)
    assert "repo_path" not in row
    assert row["checkout"] is None
