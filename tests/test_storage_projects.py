"""Tests for the LocalProjectManager storage layer."""

import sqlite3

import pytest
from gobby.storage.projects import LocalProjectManager, Project


class TestProject:
    """Tests for Project dataclass."""

    def test_from_row(self, project_manager: LocalProjectManager):
        """Test creating Project from database row."""
        # Create a project first
        project = project_manager.create(name="test-project")

        # Fetch raw row
        row = project_manager.db.fetchone("SELECT * FROM projects WHERE id = ?", (project.id,))
        assert row is not None

        # Create from row
        project_from_row = Project.from_row(row)
        assert project_from_row.id == project.id
        assert project_from_row.name == "test-project"

    def test_to_dict(self, project_manager: LocalProjectManager):
        """Test converting Project to dictionary."""
        project = project_manager.create(
            name="test-project",
            repo_path="/tmp/repo",
            github_url="https://github.com/test/repo",
        )

        d = project.to_dict()
        assert d["id"] == project.id
        assert d["name"] == "test-project"
        assert d["repo_path"] == "/tmp/repo"
        assert d["github_url"] == "https://github.com/test/repo"
        assert "created_at" in d
        assert "updated_at" in d


class TestLocalProjectManager:
    """Tests for LocalProjectManager class."""

    def test_create_project(self, project_manager: LocalProjectManager):
        """Test creating a new project."""
        project = project_manager.create(
            name="my-project",
            repo_path="/path/to/repo",
            github_url="https://github.com/user/repo",
        )

        assert project.id is not None
        assert project.name == "my-project"
        assert project.repo_path == "/path/to/repo"
        assert project.github_url == "https://github.com/user/repo"

    def test_create_project_minimal(self, project_manager: LocalProjectManager):
        """Test creating a project with only required fields."""
        project = project_manager.create(name="minimal-project")

        assert project.id is not None
        assert project.name == "minimal-project"
        assert project.repo_path is None
        assert project.github_url is None

    def test_create_duplicate_name_raises(self, project_manager: LocalProjectManager):
        """Test that creating a project with duplicate name raises error."""
        project_manager.create(name="unique-project")

        with pytest.raises(sqlite3.IntegrityError):
            project_manager.create(name="unique-project")

    def test_get_project(self, project_manager: LocalProjectManager):
        """Test getting a project by ID."""
        created = project_manager.create(name="get-test")

        retrieved = project_manager.get(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.name == "get-test"

    def test_get_nonexistent_project(self, project_manager: LocalProjectManager):
        """Test getting a nonexistent project returns None."""
        result = project_manager.get("nonexistent-id")
        assert result is None

    def test_get_by_name(self, project_manager: LocalProjectManager):
        """Test getting a project by name."""
        created = project_manager.create(name="named-project")

        retrieved = project_manager.get_by_name("named-project")
        assert retrieved is not None
        assert retrieved.id == created.id

    def test_get_by_name_nonexistent(self, project_manager: LocalProjectManager):
        """Test getting nonexistent project by name returns None."""
        result = project_manager.get_by_name("nonexistent")
        assert result is None

    def test_get_or_create_existing(self, project_manager: LocalProjectManager):
        """Test get_or_create returns existing project."""
        created = project_manager.create(name="existing-project")

        retrieved = project_manager.get_or_create(name="existing-project")
        assert retrieved.id == created.id

    def test_get_or_create_new(self, project_manager: LocalProjectManager):
        """Test get_or_create creates new project."""
        result = project_manager.get_or_create(
            name="new-project",
            repo_path="/new/path",
        )

        assert result.name == "new-project"
        assert result.repo_path == "/new/path"

    def test_list_projects(self, project_manager: LocalProjectManager):
        """Test listing all projects."""
        project_manager.create(name="alpha")
        project_manager.create(name="beta")
        project_manager.create(name="gamma")

        projects = project_manager.list()
        # Filter out migration placeholder projects
        user_projects = [p for p in projects if not p.name.startswith("_")]
        assert len(user_projects) == 3
        # Should be sorted by name
        names = [p.name for p in user_projects]
        assert names == ["alpha", "beta", "gamma"]

    def test_list_empty(self, project_manager: LocalProjectManager):
        """Test listing projects when no user projects exist."""
        projects = project_manager.list()
        # May contain migration placeholder projects (_orphaned, _migrated)
        user_projects = [p for p in projects if not p.name.startswith("_")]
        assert user_projects == []

    def test_update_project(self, project_manager: LocalProjectManager):
        """Test updating project fields."""
        created = project_manager.create(name="original")

        updated = project_manager.update(
            created.id,
            name="updated",
            repo_path="/new/path",
        )

        assert updated is not None
        assert updated.name == "updated"
        assert updated.repo_path == "/new/path"

    def test_update_partial(self, project_manager: LocalProjectManager):
        """Test updating only some fields."""
        created = project_manager.create(
            name="partial",
            repo_path="/original/path",
        )

        updated = project_manager.update(
            created.id,
            github_url="https://github.com/new/url",
        )

        assert updated is not None
        assert updated.name == "partial"  # unchanged
        assert updated.repo_path == "/original/path"  # unchanged
        assert updated.github_url == "https://github.com/new/url"

    def test_update_nonexistent(self, project_manager: LocalProjectManager):
        """Test updating nonexistent project returns None."""
        result = project_manager.update("nonexistent-id", name="new-name")
        assert result is None

    def test_update_no_fields(self, project_manager: LocalProjectManager):
        """Test update with no fields returns existing project."""
        created = project_manager.create(name="no-change")

        result = project_manager.update(created.id)
        assert result is not None
        assert result.id == created.id

    def test_update_ignores_invalid_fields(self, project_manager: LocalProjectManager):
        """Test that update ignores fields not in allowed list."""
        created = project_manager.create(name="ignore-invalid")

        result = project_manager.update(
            created.id,
            invalid_field="should be ignored",
        )

        assert result is not None
        assert result.id == created.id

    def test_delete_project(self, project_manager: LocalProjectManager):
        """Test deleting a project."""
        created = project_manager.create(name="to-delete")

        result = project_manager.delete(created.id)
        assert result is True

        # Should no longer exist
        assert project_manager.get(created.id) is None

    def test_delete_nonexistent(self, project_manager: LocalProjectManager):
        """Test deleting nonexistent project returns False."""
        result = project_manager.delete("nonexistent-id")
        assert result is False
