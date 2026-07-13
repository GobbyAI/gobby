import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.files import MAX_READ_SIZE, create_files_router

pytestmark = pytest.mark.unit

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
GIT_PROJECT_ID = "22222222-2222-4222-8222-222222222222"
GIT_DIFF_PROJECT_ID = "33333333-3333-4333-8333-333333333333"
UNKNOWN_PROJECT_ID = "99999999-9999-4999-8999-999999999999"


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
    def mock_server(self, mock_project: MagicMock) -> MagicMock:
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
        assert len(data) == 2
        assert data[0]["name"] == "Personal"
        assert data[1]["name"] == "test-project"
        assert data[1]["id"] == PROJECT_ID

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
