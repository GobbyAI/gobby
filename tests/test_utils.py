"""Tests for utility modules."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from gobby.utils.git import (
    get_git_branch,
    get_git_metadata,
    get_github_url,
    run_git_command,
)
from gobby.utils.machine_id import clear_cache, get_machine_id
from gobby.utils.project_context import (
    find_project_root,
    get_project_context,
    get_project_mcp_config_path,
    get_project_mcp_dir,
)


class TestMachineId:
    """Tests for machine_id utility."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_cache()

    def teardown_method(self):
        """Clear cache after each test."""
        clear_cache()

    def test_get_machine_id_caches_result(self):
        """Test that machine ID is cached."""
        with patch("gobby.utils.machine_id._get_or_create_machine_id") as mock_get:
            mock_get.return_value = "cached-id"

            # First call
            result1 = get_machine_id()
            # Second call should use cache
            result2 = get_machine_id()

            assert result1 == result2
            mock_get.assert_called_once()

    def test_clear_cache(self):
        """Test clearing the machine ID cache."""
        with patch("gobby.utils.machine_id._get_or_create_machine_id") as mock_get:
            mock_get.return_value = "test-id"

            get_machine_id()
            clear_cache()
            get_machine_id()

            # Should be called twice after cache clear
            assert mock_get.call_count == 2

    def test_get_machine_id_returns_string(self):
        """Test that get_machine_id returns a string."""
        with patch("gobby.utils.machine_id._get_or_create_machine_id") as mock_get:
            mock_get.return_value = "test-machine-id"
            result = get_machine_id()
            assert isinstance(result, str)
            assert len(result) > 0


class TestGitUtils:
    """Tests for git utility functions."""

    def test_run_git_command_success(self, temp_dir: Path):
        """Test running a successful git command."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)

        result = run_git_command(["git", "rev-parse", "--git-dir"], temp_dir)
        assert result is not None
        assert ".git" in result

    def test_run_git_command_not_a_repo(self, temp_dir: Path):
        """Test running git command in non-repo directory."""
        result = run_git_command(["git", "status"], temp_dir)
        assert result is None

    @patch("subprocess.run")
    def test_run_git_command_timeout(self, mock_run: MagicMock, temp_dir: Path):
        """Test git command timeout handling."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)

        result = run_git_command(["git", "status"], temp_dir, timeout=5)
        assert result is None

    @patch("subprocess.run")
    def test_run_git_command_not_found(self, mock_run: MagicMock, temp_dir: Path):
        """Test git executable not found."""
        mock_run.side_effect = FileNotFoundError()

        result = run_git_command(["git", "status"], temp_dir)
        assert result is None

    def test_get_github_url_with_origin(self, temp_dir: Path):
        """Test getting GitHub URL from origin remote."""
        # Initialize git repo with remote
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        result = get_github_url(temp_dir)
        assert result == "https://github.com/test/repo.git"

    def test_get_github_url_no_remote(self, temp_dir: Path):
        """Test getting GitHub URL when no remote exists."""
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)

        result = get_github_url(temp_dir)
        assert result is None

    def test_get_git_branch(self, temp_dir: Path):
        """Test getting current git branch."""
        # Initialize git repo and create initial commit
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        # Create a file and commit
        (temp_dir / "test.txt").write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        result = get_git_branch(temp_dir)
        # Modern git uses 'main' or 'master' as default
        assert result in ["main", "master"]

    def test_get_git_branch_not_a_repo(self, temp_dir: Path):
        """Test getting branch in non-repo directory."""
        result = get_git_branch(temp_dir)
        assert result is None

    def test_get_git_metadata(self, temp_dir: Path):
        """Test getting comprehensive git metadata."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        # Create initial commit
        (temp_dir / "test.txt").write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        metadata = get_git_metadata(temp_dir)
        assert metadata.get("github_url") == "https://github.com/test/repo.git"
        assert metadata.get("git_branch") in ["main", "master"]

    def test_get_git_metadata_not_a_repo(self, temp_dir: Path):
        """Test getting metadata from non-repo directory."""
        metadata = get_git_metadata(temp_dir)
        assert metadata == {}

    def test_get_git_metadata_nonexistent_path(self):
        """Test getting metadata from nonexistent path."""
        metadata = get_git_metadata(Path("/nonexistent/path"))
        assert metadata == {}


class TestProjectContext:
    """Tests for project context utilities."""

    def test_find_project_root(self, temp_dir: Path):
        """Test finding project root with .gobby/project.json."""
        # Create project structure
        gobby_dir = temp_dir / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text('{"id": "test-id"}')

        # Test from project root - resolve both to handle macOS symlinks
        result = find_project_root(temp_dir)
        assert result is not None
        assert result.resolve() == temp_dir.resolve()

        # Test from subdirectory
        subdir = temp_dir / "src" / "lib"
        subdir.mkdir(parents=True)
        result = find_project_root(subdir)
        assert result is not None
        assert result.resolve() == temp_dir.resolve()

    def test_find_project_root_not_found(self, temp_dir: Path):
        """Test finding project root when not in a project."""
        result = find_project_root(temp_dir)
        assert result is None

    def test_get_project_context(self, temp_dir: Path):
        """Test getting project context."""
        # Create project structure
        gobby_dir = temp_dir / ".gobby"
        gobby_dir.mkdir()
        project_data = {"id": "test-id", "name": "test-project"}
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_project_context(temp_dir)
        assert result is not None
        assert result["id"] == "test-id"
        assert result["name"] == "test-project"
        # Handle macOS symlinks (/var -> /private/var)
        assert Path(result["project_path"]).resolve() == temp_dir.resolve()

    def test_get_project_context_not_found(self, temp_dir: Path):
        """Test getting project context when not in a project."""
        result = get_project_context(temp_dir)
        assert result is None

    def test_get_project_context_invalid_json(self, temp_dir: Path):
        """Test getting project context with invalid JSON."""
        gobby_dir = temp_dir / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text("invalid json")

        result = get_project_context(temp_dir)
        assert result is None

    def test_get_project_mcp_dir(self):
        """Test getting project MCP directory path."""
        result = get_project_mcp_dir("My Project")
        expected = Path.home() / ".gobby" / "projects" / "my_project"
        assert result == expected

    def test_get_project_mcp_config_path(self):
        """Test getting project MCP config path."""
        result = get_project_mcp_config_path("test-project")
        expected = Path.home() / ".gobby" / "projects" / "test-project" / ".mcp.json"
        assert result == expected


class TestMigrations:
    """Tests for database migrations."""

    def test_migrations_run_in_order(self, temp_dir: Path):
        """Test that migrations run in version order."""
        from gobby.storage.database import LocalDatabase
        from gobby.storage.migrations import get_current_version, run_migrations

        db = LocalDatabase(temp_dir / "test.db")

        # Before migrations
        assert get_current_version(db) == 0

        # Run migrations
        applied = run_migrations(db)
        assert applied > 0

        # After migrations
        current_version = get_current_version(db)
        assert current_version > 0

        db.close()

    def test_migrations_are_idempotent(self, temp_dir: Path):
        """Test that running migrations twice doesn't fail."""
        from gobby.storage.database import LocalDatabase
        from gobby.storage.migrations import get_current_version, run_migrations

        db = LocalDatabase(temp_dir / "test.db")

        # First run
        run_migrations(db)
        version1 = get_current_version(db)

        # Second run
        applied = run_migrations(db)
        version2 = get_current_version(db)

        assert applied == 0  # No new migrations
        assert version1 == version2

        db.close()

    def test_tables_created(self, temp_db):
        """Test that expected tables are created."""
        # Check tables exist
        tables = temp_db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        table_names = [t["name"] for t in tables]

        assert "schema_version" in table_names
        assert "projects" in table_names
        assert "sessions" in table_names
        assert "mcp_servers" in table_names
        assert "tools" in table_names
