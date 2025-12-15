"""Tests for the project initialization utilities."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.project_init import InitResult, _write_project_json


class TestInitResult:
    """Tests for InitResult dataclass."""

    def test_init_result_creation(self):
        """Test creating InitResult with all fields."""
        result = InitResult(
            project_id="proj-123",
            project_name="my-project",
            project_path="/path/to/project",
            created_at="2024-01-01T00:00:00Z",
            already_existed=False,
        )

        assert result.project_id == "proj-123"
        assert result.project_name == "my-project"
        assert result.project_path == "/path/to/project"
        assert result.created_at == "2024-01-01T00:00:00Z"
        assert result.already_existed is False

    def test_init_result_already_existed(self):
        """Test InitResult with already_existed=True."""
        result = InitResult(
            project_id="existing-proj",
            project_name="existing",
            project_path="/path",
            created_at="2023-01-01T00:00:00Z",
            already_existed=True,
        )

        assert result.already_existed is True


class TestWriteProjectJson:
    """Tests for _write_project_json function."""

    def test_creates_gobby_dir(self, tmp_path: Path):
        """Test that .gobby directory is created if it doesn't exist."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        _write_project_json(cwd, "proj-id", "test-project", "2024-01-01")

        gobby_dir = cwd / ".gobby"
        assert gobby_dir.exists()
        assert gobby_dir.is_dir()

    def test_writes_project_json(self, tmp_path: Path):
        """Test that project.json is written with correct content."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        _write_project_json(cwd, "proj-123", "my-project", "2024-06-15T12:00:00Z")

        project_file = cwd / ".gobby" / "project.json"
        assert project_file.exists()

        content = json.loads(project_file.read_text())
        assert content["id"] == "proj-123"
        assert content["name"] == "my-project"
        assert content["created_at"] == "2024-06-15T12:00:00Z"

    def test_overwrites_existing_project_json(self, tmp_path: Path):
        """Test that existing project.json is overwritten."""
        cwd = tmp_path / "project"
        cwd.mkdir()
        gobby_dir = cwd / ".gobby"
        gobby_dir.mkdir()

        # Write initial content
        project_file = gobby_dir / "project.json"
        project_file.write_text(json.dumps({"id": "old-id"}))

        # Overwrite
        _write_project_json(cwd, "new-id", "new-name", "2024-01-01")

        content = json.loads(project_file.read_text())
        assert content["id"] == "new-id"
        assert content["name"] == "new-name"

    def test_handles_existing_gobby_dir(self, tmp_path: Path):
        """Test that existing .gobby directory is handled correctly."""
        cwd = tmp_path / "project"
        cwd.mkdir()
        gobby_dir = cwd / ".gobby"
        gobby_dir.mkdir()

        # Should not raise even if dir exists
        _write_project_json(cwd, "proj-id", "name", "2024-01-01")

        assert (gobby_dir / "project.json").exists()


class TestInitializeProject:
    """Tests for initialize_project function."""

    def test_already_initialized_returns_existing(self, tmp_path: Path):
        """Test that already initialized project returns existing info."""
        from gobby.utils.project_init import initialize_project

        # Patch at the source modules where they are imported from
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {
                "id": "existing-id",
                "name": "existing-name",
                "project_path": str(tmp_path),
                "created_at": "2024-01-01",
            }

            result = initialize_project(tmp_path)

            assert result.project_id == "existing-id"
            assert result.project_name == "existing-name"
            assert result.already_existed is True

    def test_new_project_creation(self, tmp_path: Path):
        """Test creating a new project."""
        from gobby.utils.project_init import initialize_project

        # Patch all the imports used inside the function
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.database.LocalDatabase"):
                    with patch("gobby.storage.migrations.run_migrations"):
                        with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                            mock_pm_instance = MagicMock()
                            mock_pm_instance.get_by_name.return_value = None

                            mock_project = MagicMock()
                            mock_project.id = "new-proj-id"
                            mock_project.name = tmp_path.name
                            mock_project.created_at = "2024-06-15T00:00:00Z"
                            mock_pm_instance.create.return_value = mock_project

                            mock_pm_cls.return_value = mock_pm_instance

                            result = initialize_project(tmp_path)

                            assert result.project_id == "new-proj-id"
                            assert result.project_name == tmp_path.name
                            assert result.already_existed is False

    def test_uses_provided_name(self, tmp_path: Path):
        """Test that provided name overrides directory name."""
        from gobby.utils.project_init import initialize_project

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.database.LocalDatabase"):
                    with patch("gobby.storage.migrations.run_migrations"):
                        with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                            mock_pm_instance = MagicMock()
                            mock_pm_instance.get_by_name.return_value = None

                            mock_project = MagicMock()
                            mock_project.id = "id"
                            mock_project.name = "custom-name"
                            mock_project.created_at = "2024-01-01"
                            mock_pm_instance.create.return_value = mock_project

                            mock_pm_cls.return_value = mock_pm_instance

                            result = initialize_project(tmp_path, name="custom-name")

                            call_kwargs = mock_pm_instance.create.call_args
                            assert call_kwargs.kwargs["name"] == "custom-name"

    def test_uses_provided_github_url(self, tmp_path: Path):
        """Test that provided github_url is used."""
        from gobby.utils.project_init import initialize_project

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value="https://auto-detected.com"):
                with patch("gobby.storage.database.LocalDatabase"):
                    with patch("gobby.storage.migrations.run_migrations"):
                        with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                            mock_pm_instance = MagicMock()
                            mock_pm_instance.get_by_name.return_value = None

                            mock_project = MagicMock()
                            mock_project.id = "id"
                            mock_project.name = "name"
                            mock_project.created_at = "2024-01-01"
                            mock_pm_instance.create.return_value = mock_project

                            mock_pm_cls.return_value = mock_pm_instance

                            result = initialize_project(
                                tmp_path, github_url="https://github.com/custom/repo"
                            )

                            call_kwargs = mock_pm_instance.create.call_args
                            assert call_kwargs.kwargs["github_url"] == "https://github.com/custom/repo"

    def test_auto_detects_github_url(self, tmp_path: Path):
        """Test that github URL is auto-detected from git remote."""
        from gobby.utils.project_init import initialize_project

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch(
                "gobby.utils.git.get_github_url", return_value="https://github.com/detected/repo"
            ):
                with patch("gobby.storage.database.LocalDatabase"):
                    with patch("gobby.storage.migrations.run_migrations"):
                        with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                            mock_pm_instance = MagicMock()
                            mock_pm_instance.get_by_name.return_value = None

                            mock_project = MagicMock()
                            mock_project.id = "id"
                            mock_project.name = "name"
                            mock_project.created_at = "2024-01-01"
                            mock_pm_instance.create.return_value = mock_project

                            mock_pm_cls.return_value = mock_pm_instance

                            result = initialize_project(tmp_path)

                            call_kwargs = mock_pm_instance.create.call_args
                            assert call_kwargs.kwargs["github_url"] == "https://github.com/detected/repo"

    def test_existing_db_project_no_local_json(self, tmp_path: Path):
        """Test handling when project exists in DB but no local project.json."""
        from gobby.utils.project_init import initialize_project

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.database.LocalDatabase"):
                    with patch("gobby.storage.migrations.run_migrations"):
                        with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                            # Project exists in database
                            mock_existing = MagicMock()
                            mock_existing.id = "db-proj-id"
                            mock_existing.name = tmp_path.name
                            mock_existing.created_at = "2023-01-01T00:00:00Z"

                            mock_pm_instance = MagicMock()
                            mock_pm_instance.get_by_name.return_value = mock_existing

                            mock_pm_cls.return_value = mock_pm_instance

                            result = initialize_project(tmp_path)

                            # Should return existing project and write local json
                            assert result.project_id == "db-proj-id"
                            assert result.already_existed is True

                            # Should write project.json
                            project_file = tmp_path / ".gobby" / "project.json"
                            assert project_file.exists()

                            # Should NOT call create
                            mock_pm_instance.create.assert_not_called()

    def test_uses_cwd_when_none(self):
        """Test that current working directory is used when cwd is None."""
        from gobby.utils.project_init import initialize_project

        mock_project_context = {"id": "id", "name": "name", "project_path": "/test", "created_at": "2024"}

        with patch("gobby.utils.project_context.get_project_context", return_value=mock_project_context):
            with patch("pathlib.Path.cwd") as mock_cwd:
                mock_cwd.return_value = Path("/some/path")

                result = initialize_project(cwd=None)

                # Should use cwd
                assert result.project_id == "id"
