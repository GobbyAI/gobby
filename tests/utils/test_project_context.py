"""Comprehensive tests for the project_context utilities."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.project_context import (
    _build_and_set_project_context,
    _current_project_context,
    ensure_project_json_for_isolation,
    find_project_root,
    get_project_context,
    get_project_mcp_config_path,
    get_project_mcp_dir,
    get_verification_config,
    reset_project_context,
    set_project_context,
    set_project_context_from_ref,
    set_project_context_from_session,
)

pytestmark = pytest.mark.unit


class TestFindProjectRoot:
    """Tests for find_project_root function."""

    def test_find_project_root_from_project_dir(self, tmp_path: Path) -> None:
        """Test finding project root when starting from project directory."""
        # Create project structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text('{"id": "test-id"}')

        result = find_project_root(tmp_path)
        assert result is not None
        # Handle macOS symlinks (/var -> /private/var)
        assert result.resolve() == tmp_path.resolve()

    def test_find_project_root_from_nested_subdir(self, tmp_path: Path) -> None:
        """Test finding project root from deeply nested subdirectory."""
        # Create project structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text('{"id": "test-id"}')

        # Create deep nested directory
        deep_subdir = tmp_path / "src" / "lib" / "utils" / "helpers"
        deep_subdir.mkdir(parents=True)

        result = find_project_root(deep_subdir)
        assert result is not None
        assert result.resolve() == tmp_path.resolve()

    def test_find_project_root_not_found(self, tmp_path: Path, monkeypatch) -> None:
        """Test finding project root when no .gobby/project.json exists."""
        # Isolate test from parent directories by making exists() return False for paths outside tmp_path
        original_exists = Path.exists

        def isolated_exists(self):
            # Only check existence for paths under tmp_path
            try:
                self.relative_to(tmp_path)
                return original_exists(self)
            except ValueError:
                return False

        monkeypatch.setattr(Path, "exists", isolated_exists)
        result = find_project_root(tmp_path)
        assert result is None

    def test_find_project_root_with_none_uses_cwd(self) -> None:
        """Test that None cwd defaults to current working directory."""
        with patch("pathlib.Path.cwd") as mock_cwd:
            mock_path = MagicMock(spec=Path)
            mock_path.resolve.return_value = mock_path
            mock_path.parents = []
            mock_path.__truediv__ = MagicMock(return_value=MagicMock())
            # Make the project.json check return False (not found)
            project_json_mock = MagicMock()
            project_json_mock.exists.return_value = False
            mock_path.__truediv__.return_value.__truediv__ = MagicMock(
                return_value=project_json_mock
            )

            mock_cwd.return_value = mock_path

            result = find_project_root(None)

            mock_cwd.assert_called_once()
            assert result is None

    def test_find_project_root_gobby_dir_exists_but_no_project_json(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Test that .gobby dir without project.json is not considered a project root."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        # No project.json file

        # Isolate test from parent directories
        original_exists = Path.exists

        def isolated_exists(self):
            try:
                self.relative_to(tmp_path)
                return original_exists(self)
            except ValueError:
                return False

        monkeypatch.setattr(Path, "exists", isolated_exists)
        result = find_project_root(tmp_path)
        assert result is None

    def test_find_project_root_at_filesystem_root(self, tmp_path: Path, monkeypatch) -> None:
        """Test finding project root traverses up to filesystem root without error."""
        # Create directory without .gobby
        subdir = tmp_path / "some" / "path"
        subdir.mkdir(parents=True)

        # Isolate test from parent directories
        original_exists = Path.exists

        def isolated_exists(self):
            try:
                self.relative_to(tmp_path)
                return original_exists(self)
            except ValueError:
                return False

        monkeypatch.setattr(Path, "exists", isolated_exists)
        # Should return None after traversing to filesystem root
        result = find_project_root(subdir)
        assert result is None


class TestGetProjectContext:
    """Tests for get_project_context function."""

    def test_get_project_context_success(self, tmp_path: Path) -> None:
        """Test getting project context with valid project.json."""
        # Create project structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "created_at": "2024-01-01T00:00:00Z",
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_project_context(tmp_path)

        assert result is not None
        assert result["id"] == "test-id"
        assert result["name"] == "test-project"
        assert result["created_at"] == "2024-01-01T00:00:00Z"
        # project_path should be added
        assert "project_path" in result
        assert Path(result["project_path"]).resolve() == tmp_path.resolve()

    def test_get_project_context_not_found(self, tmp_path: Path, monkeypatch) -> None:
        """Test getting project context when no project exists."""
        # Isolate test from parent directories
        original_exists = Path.exists

        def isolated_exists(self):
            try:
                self.relative_to(tmp_path)
                return original_exists(self)
            except ValueError:
                return False

        monkeypatch.setattr(Path, "exists", isolated_exists)
        result = get_project_context(tmp_path)
        assert result is None

    def test_get_project_context_invalid_json(self, tmp_path: Path) -> None:
        """Test getting project context with malformed JSON."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text("this is not valid json {{{")

        result = get_project_context(tmp_path)
        assert result is None

    def test_get_project_context_empty_file(self, tmp_path: Path) -> None:
        """Test getting project context with empty project.json file."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text("")

        result = get_project_context(tmp_path)
        assert result is None

    def test_get_project_context_with_verification(self, tmp_path: Path) -> None:
        """Test getting project context that includes verification config."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "verification": {
                "unit_tests": "pytest tests/",
                "type_check": "mypy src/",
                "lint": "ruff check src/",
            },
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_project_context(tmp_path)

        assert result is not None
        assert "verification" in result
        assert result["verification"]["unit_tests"] == "pytest tests/"
        assert result["verification"]["type_check"] == "mypy src/"

    def test_get_project_context_permission_error(self, tmp_path: Path) -> None:
        """Test getting project context when file read fails."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_file = gobby_dir / "project.json"
        project_file.write_text('{"id": "test"}')

        with patch("builtins.open", side_effect=PermissionError("Access denied")):
            result = get_project_context(tmp_path)

        assert result is None

    def test_get_project_context_from_subdirectory(self, tmp_path: Path) -> None:
        """Test getting project context from a subdirectory."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {"id": "parent-id", "name": "parent-project"}
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        subdir = tmp_path / "src" / "components"
        subdir.mkdir(parents=True)

        result = get_project_context(subdir)

        assert result is not None
        assert result["id"] == "parent-id"
        assert result["name"] == "parent-project"


class TestGetProjectMcpDir:
    """Tests for get_project_mcp_dir function."""

    def test_get_project_mcp_dir_simple_name(self) -> None:
        """Test getting MCP directory with simple project name."""
        result = get_project_mcp_dir("myproject")
        expected = Path.home() / ".gobby" / "projects" / "myproject"
        assert result == expected

    def test_get_project_mcp_dir_with_spaces(self) -> None:
        """Test that spaces in project name are converted to underscores."""
        result = get_project_mcp_dir("My Project Name")
        expected = Path.home() / ".gobby" / "projects" / "my_project_name"
        assert result == expected

    def test_get_project_mcp_dir_already_lowercase(self) -> None:
        """Test that lowercase names remain unchanged except spaces."""
        result = get_project_mcp_dir("test project")
        expected = Path.home() / ".gobby" / "projects" / "test_project"
        assert result == expected

    def test_get_project_mcp_dir_mixed_case(self) -> None:
        """Test that mixed case is converted to lowercase."""
        result = get_project_mcp_dir("MyProjectName")
        expected = Path.home() / ".gobby" / "projects" / "myprojectname"
        assert result == expected

    def test_get_project_mcp_dir_with_dashes(self) -> None:
        """Test that dashes are preserved in project name."""
        result = get_project_mcp_dir("my-project")
        expected = Path.home() / ".gobby" / "projects" / "my-project"
        assert result == expected

    def test_get_project_mcp_dir_with_underscores(self) -> None:
        """Test that underscores are preserved in project name."""
        result = get_project_mcp_dir("my_project")
        expected = Path.home() / ".gobby" / "projects" / "my_project"
        assert result == expected

    def test_get_project_mcp_dir_empty_string(self) -> None:
        """Test handling of empty project name."""
        result = get_project_mcp_dir("")
        expected = Path.home() / ".gobby" / "projects" / ""
        assert result == expected


class TestGetProjectMcpConfigPath:
    """Tests for get_project_mcp_config_path function."""

    def test_get_project_mcp_config_path_simple(self) -> None:
        """Test getting MCP config path with simple name."""
        result = get_project_mcp_config_path("test-project")
        expected = Path.home() / ".gobby" / "projects" / "test-project" / ".mcp.json"
        assert result == expected

    def test_get_project_mcp_config_path_with_spaces(self) -> None:
        """Test that spaces are handled in config path."""
        result = get_project_mcp_config_path("Test Project")
        expected = Path.home() / ".gobby" / "projects" / "test_project" / ".mcp.json"
        assert result == expected

    def test_get_project_mcp_config_path_uses_get_project_mcp_dir(self) -> None:
        """Test that config path is built on top of MCP dir."""
        project_name = "sample-project"
        dir_result = get_project_mcp_dir(project_name)
        config_result = get_project_mcp_config_path(project_name)

        assert config_result == dir_result / ".mcp.json"


class TestGetVerificationConfig:
    """Tests for get_verification_config function."""

    def test_get_verification_config_success(self, tmp_path: Path) -> None:
        """Test getting verification config with valid data."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "verification": {
                "unit_tests": "uv run pytest tests/ -v",
                "type_check": "uv run mypy src/",
                "lint": "uv run ruff check src/",
                "integration": "uv run pytest tests/integration/",
                "custom": {"e2e": "playwright test"},
            },
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_verification_config(tmp_path)

        assert result is not None
        assert result.unit_tests == "uv run pytest tests/ -v"
        assert result.type_check == "uv run mypy src/"
        assert result.lint == "uv run ruff check src/"
        assert result.integration == "uv run pytest tests/integration/"
        assert result.custom == {"e2e": "playwright test"}

    def test_get_verification_config_partial_fields(self, tmp_path: Path) -> None:
        """Test getting verification config with only some fields populated."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "verification": {
                "unit_tests": "pytest",
            },
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_verification_config(tmp_path)

        assert result is not None
        assert result.unit_tests == "pytest"
        assert result.type_check is None
        assert result.lint is None
        assert result.integration is None
        assert result.custom == {}

    def test_get_verification_config_empty_verification(self, tmp_path: Path) -> None:
        """Test getting verification config with empty verification section."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "verification": {},
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_verification_config(tmp_path)

        # Empty dict is falsy, so the function returns None
        # (the code checks `if not verification_data:`)
        assert result is None

    def test_get_verification_config_no_verification_section(self, tmp_path: Path) -> None:
        """Test getting verification config when verification key is missing."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_verification_config(tmp_path)
        assert result is None

    def test_get_verification_config_no_project(self, tmp_path: Path) -> None:
        """Test getting verification config when no project exists."""
        result = get_verification_config(tmp_path)
        assert result is None

    def test_get_verification_config_invalid_verification_data(self, tmp_path: Path) -> None:
        """Test getting verification config with invalid verification structure."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "verification": {
                "unit_tests": 12345,  # Should be string or None
                "custom": "not a dict",  # Should be dict
            },
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_verification_config(tmp_path)
        # Pydantic validation should fail, returning None
        assert result is None

    def test_get_verification_config_null_verification(self, tmp_path: Path) -> None:
        """Test getting verification config when verification is null."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "verification": None,
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_verification_config(tmp_path)
        assert result is None

    def test_get_verification_config_with_none_cwd(self) -> None:
        """Test get_verification_config with None cwd parameter."""
        with patch(
            "gobby.utils.project_context.get_project_context", return_value=None
        ) as mock_ctx:
            result = get_verification_config(None)

            mock_ctx.assert_called_once_with(None)
            assert result is None

    def test_get_verification_config_custom_commands(self, tmp_path: Path) -> None:
        """Test verification config with multiple custom commands."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "verification": {
                "custom": {
                    "security": "bandit -r src/",
                    "format": "black --check src/",
                    "docs": "mkdocs build --strict",
                },
            },
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_verification_config(tmp_path)

        assert result is not None
        assert len(result.custom) == 3
        assert result.custom["security"] == "bandit -r src/"
        assert result.custom["format"] == "black --check src/"
        assert result.custom["docs"] == "mkdocs build --strict"


class TestGetProjectContextEnvOverride:
    """Tests for GOBBY_PROJECT_ID environment variable override in get_project_context."""

    def test_env_override_returns_minimal_context(self, tmp_path: Path, monkeypatch) -> None:
        """When GOBBY_PROJECT_ID is set but no CWD project exists, return minimal context."""
        monkeypatch.setenv("GOBBY_PROJECT_ID", "override-project-123")

        # Isolate from real project roots
        original_exists = Path.exists

        def isolated_exists(self):
            try:
                self.relative_to(tmp_path)
                return original_exists(self)
            except ValueError:
                return False

        monkeypatch.setattr(Path, "exists", isolated_exists)

        result = get_project_context(tmp_path)
        assert result is not None
        assert result == {"id": "override-project-123"}

    def test_env_override_matching_cwd_returns_full_context(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When GOBBY_PROJECT_ID matches CWD project, return full context."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "matching-id",
            "name": "My Project",
            "created_at": "2024-01-01T00:00:00Z",
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        monkeypatch.setenv("GOBBY_PROJECT_ID", "matching-id")

        result = get_project_context(tmp_path)
        assert result is not None
        assert result["id"] == "matching-id"
        assert result["name"] == "My Project"
        assert "project_path" in result
        assert Path(result["project_path"]).resolve() == tmp_path.resolve()

    def test_env_override_mismatched_cwd_returns_minimal(self, tmp_path: Path, monkeypatch) -> None:
        """When GOBBY_PROJECT_ID differs from CWD project, return minimal context."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {"id": "cwd-project-id", "name": "CWD Project"}
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        monkeypatch.setenv("GOBBY_PROJECT_ID", "different-project-id")

        result = get_project_context(tmp_path)
        assert result is not None
        assert result == {"id": "different-project-id"}

    def test_no_env_override_uses_cwd(self, tmp_path: Path, monkeypatch) -> None:
        """Without GOBBY_PROJECT_ID, standard CWD-based resolution is used."""
        monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)

        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {"id": "cwd-id", "name": "CWD Project"}
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_project_context(tmp_path)
        assert result is not None
        assert result["id"] == "cwd-id"
        assert result["name"] == "CWD Project"
        assert "project_path" in result


class TestContextVarBehavior:
    """Tests for context var vs filesystem resolution in get_project_context."""

    def test_context_var_ignored_when_explicit_cwd(self, tmp_path: Path) -> None:
        """When cwd is provided, the context var should be skipped."""
        # Set up filesystem project
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {"id": "filesystem-project", "name": "FS Project"}
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        # Set context var to a different project
        token = _current_project_context.set(
            {"id": "context-var-project", "name": "CV Project", "project_path": "/fake"}
        )
        try:
            result = get_project_context(tmp_path)
            assert result is not None
            assert result["id"] == "filesystem-project"
            assert result["name"] == "FS Project"
        finally:
            _current_project_context.reset(token)

    def test_context_var_used_when_cwd_is_none(self) -> None:
        """When cwd is None, the context var should be used if set."""
        ctx = {"id": "context-var-project", "name": "CV Project", "project_path": "/fake"}
        token = _current_project_context.set(ctx)
        try:
            result = get_project_context()
            assert result is not None
            assert result["id"] == "context-var-project"
            assert result["name"] == "CV Project"
        finally:
            _current_project_context.reset(token)

    def test_set_project_context_blocks_test_ids_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test project IDs like 'e2e-test-project' are blocked in production."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

        for test_id in ("e2e-test-project", "test-project"):
            token = set_project_context({"id": test_id, "name": "Bad"})
            try:
                assert _current_project_context.get() is None
            finally:
                _current_project_context.reset(token)

    def test_set_project_context_allows_test_ids_in_tests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With GOBBY_TEST_PROTECT=1, test project IDs are allowed."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

        ctx = {"id": "e2e-test-project", "name": "Test"}
        token = set_project_context(ctx)
        try:
            assert _current_project_context.get() is not None
            assert _current_project_context.get()["id"] == "e2e-test-project"
        finally:
            _current_project_context.reset(token)

    def test_set_project_context_allows_real_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Real project IDs are always allowed regardless of GOBBY_TEST_PROTECT."""
        monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

        ctx = {"id": "d45545c5-ded5-4335-b115-0245752edacf", "name": "Real Project"}
        token = set_project_context(ctx)
        try:
            assert _current_project_context.get() is not None
            assert _current_project_context.get()["id"] == "d45545c5-ded5-4335-b115-0245752edacf"
        finally:
            _current_project_context.reset(token)

    def test_set_project_context_allows_none(self) -> None:
        """Setting None context should always work."""
        token = set_project_context(None)
        try:
            assert _current_project_context.get() is None
        finally:
            _current_project_context.reset(token)


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_project_json_is_directory(self, tmp_path: Path) -> None:
        """Test handling when project.json is a directory instead of file."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        # Create project.json as a directory
        project_json_dir = gobby_dir / "project.json"
        project_json_dir.mkdir()

        result = get_project_context(tmp_path)
        assert result is None

    def test_unicode_project_name(self, tmp_path: Path) -> None:
        """Test project context with unicode characters in name."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "My Project with emoji and unicode characters",
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data, ensure_ascii=False))

        result = get_project_context(tmp_path)
        assert result is not None
        assert "emoji" in result["name"]

    def test_large_project_json(self, tmp_path: Path) -> None:
        """Test handling large project.json file."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {
            "id": "test-id",
            "name": "test-project",
            "extra_data": "x" * 100000,  # Large string
        }
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        result = get_project_context(tmp_path)
        assert result is not None
        assert result["id"] == "test-id"

    def test_symlinked_gobby_dir(self, tmp_path: Path) -> None:
        """Test finding project root with symlinked .gobby directory."""
        # Create actual .gobby dir elsewhere
        actual_gobby = tmp_path / "actual_gobby"
        actual_gobby.mkdir()
        (actual_gobby / "project.json").write_text('{"id": "symlink-test"}')

        # Create project dir with symlink
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        symlink_path = project_dir / ".gobby"
        symlink_path.symlink_to(actual_gobby)

        result = find_project_root(project_dir)
        assert result is not None
        assert result.resolve() == project_dir.resolve()

    def test_read_reflects_updated_file(self, tmp_path: Path) -> None:
        """Test that reading project context reflects file updates."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_file = gobby_dir / "project.json"
        project_file.write_text('{"id": "original"}')

        # Read should return consistent data
        result1 = get_project_context(tmp_path)
        assert result1 is not None
        assert result1["id"] == "original"

        # Update file
        project_file.write_text('{"id": "updated"}')

        # Next read should get updated data
        result2 = get_project_context(tmp_path)
        assert result2 is not None
        assert result2["id"] == "updated"

    def test_special_characters_in_path(self, tmp_path: Path) -> None:
        """Test handling paths with special characters."""
        # Create directory with special characters
        special_dir = tmp_path / "project with spaces & special (chars)"
        special_dir.mkdir()
        gobby_dir = special_dir / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text('{"id": "special"}')

        result = find_project_root(special_dir)
        assert result is not None

        context = get_project_context(special_dir)
        assert context is not None
        assert context["id"] == "special"


class TestEnsureProjectJsonForIsolation:
    """Tests for ensure_project_json_for_isolation."""

    def test_creates_when_missing(self, tmp_path: Path) -> None:
        """Target has no project.json — copies from source with parent_project_path."""
        repo = tmp_path / "repo"
        (repo / ".gobby").mkdir(parents=True)
        (repo / ".gobby" / "project.json").write_text('{"id": "proj-1", "name": "test"}')

        target = tmp_path / "worktree"
        target.mkdir()

        ensure_project_json_for_isolation(repo, target)

        result = json.loads((target / ".gobby" / "project.json").read_text())
        assert result["id"] == "proj-1"
        assert result["name"] == "test"
        assert result["parent_project_path"] == str(repo.resolve())
        assert result["parent_project_id"] == "proj-1"

    def test_augments_existing(self, tmp_path: Path) -> None:
        """Target already has project.json (git-tracked) — overwrites with parent_project_path."""
        repo = tmp_path / "repo"
        (repo / ".gobby").mkdir(parents=True)
        (repo / ".gobby" / "project.json").write_text('{"id": "proj-1", "name": "test"}')

        target = tmp_path / "worktree"
        (target / ".gobby").mkdir(parents=True)
        # Simulate git-tracked file without parent_project_path
        (target / ".gobby" / "project.json").write_text('{"id": "proj-1", "name": "test"}')

        ensure_project_json_for_isolation(repo, target)

        result = json.loads((target / ".gobby" / "project.json").read_text())
        assert result["id"] == "proj-1"
        assert result["parent_project_path"] == str(repo.resolve())
        assert result["parent_project_id"] == "proj-1"

    def test_noop_when_source_missing(self, tmp_path: Path) -> None:
        """Source has no project.json — does nothing."""
        repo = tmp_path / "repo"
        repo.mkdir()

        target = tmp_path / "worktree"
        target.mkdir()

        ensure_project_json_for_isolation(repo, target)

        assert not (target / ".gobby" / "project.json").exists()

    def test_handles_errors_gracefully(self, tmp_path: Path) -> None:
        """Filesystem error during write — logs warning, doesn't raise."""
        repo = tmp_path / "repo"
        (repo / ".gobby").mkdir(parents=True)
        (repo / ".gobby" / "project.json").write_text('{"id": "proj-1"}')

        # Use a path that can't be written to
        target = tmp_path / "worktree"
        target.mkdir()
        gobby_dir = target / ".gobby"
        gobby_dir.mkdir()
        # Make directory read-only to force write failure
        gobby_dir.chmod(0o444)

        try:
            # Should not raise
            ensure_project_json_for_isolation(repo, target)
        finally:
            gobby_dir.chmod(0o755)


class TestBuildAndSetProjectContext:
    """Tests for _build_and_set_project_context helper."""

    def test_sets_basic_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that basic project fields are set correctly."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        project = MagicMock()
        project.id = "proj-123"
        project.name = "my-project"
        project.repo_path = None

        token = _build_and_set_project_context(project)
        try:
            ctx = _current_project_context.get()
            assert ctx is not None
            assert ctx["id"] == "proj-123"
            assert ctx["name"] == "my-project"
            assert ctx["project_path"] is None
        finally:
            _current_project_context.reset(token)

    def test_normalizes_empty_repo_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty/whitespace repo_path is normalized to None."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        project = MagicMock()
        project.id = "proj-123"
        project.name = "system-proj"
        project.repo_path = "  "

        token = _build_and_set_project_context(project)
        try:
            ctx = _current_project_context.get()
            assert ctx is not None
            assert ctx["project_path"] is None
        finally:
            _current_project_context.reset(token)

    def test_enriches_from_project_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enriches context from .gobby/project.json when available."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_data = {"id": "proj-123", "name": "my-project", "extra_field": "bonus"}
        (gobby_dir / "project.json").write_text(json.dumps(project_data))

        project = MagicMock()
        project.id = "proj-123"
        project.name = "my-project"
        project.repo_path = str(tmp_path)

        token = _build_and_set_project_context(project)
        try:
            ctx = _current_project_context.get()
            assert ctx is not None
            assert ctx["extra_field"] == "bonus"
            assert ctx["project_path"] == str(tmp_path)
        finally:
            _current_project_context.reset(token)

    def test_warns_on_id_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Warns and uses filesystem ID when DB and filesystem disagree."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        (gobby_dir / "project.json").write_text(json.dumps({"id": "fs-id", "name": "my-project"}))

        project = MagicMock()
        project.id = "db-id"
        project.name = "my-project"
        project.repo_path = str(tmp_path)

        token = _build_and_set_project_context(project)
        try:
            ctx = _current_project_context.get()
            assert ctx is not None
            # Filesystem wins
            assert ctx["id"] == "fs-id"
        finally:
            _current_project_context.reset(token)


class TestSetProjectContextFromRef:
    """Tests for set_project_context_from_ref."""

    def test_resolves_by_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolves a project by UUID and sets context."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        mock_project = MagicMock()
        mock_project.id = "uuid-123"
        mock_project.name = "test-proj"
        mock_project.repo_path = None

        mock_db = MagicMock()
        with patch("gobby.storage.projects.LocalProjectManager") as MockPM:
            MockPM.return_value.resolve_ref.return_value = mock_project
            token = set_project_context_from_ref("uuid-123", mock_db)

        assert token is not None
        try:
            ctx = _current_project_context.get()
            assert ctx is not None
            assert ctx["id"] == "uuid-123"
            assert ctx["name"] == "test-proj"
        finally:
            reset_project_context(token)

    def test_resolves_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolves a project by name and sets context."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        mock_project = MagicMock()
        mock_project.id = "uuid-456"
        mock_project.name = "gobby"
        mock_project.repo_path = None

        mock_db = MagicMock()
        with patch("gobby.storage.projects.LocalProjectManager") as MockPM:
            MockPM.return_value.resolve_ref.return_value = mock_project
            token = set_project_context_from_ref("gobby", mock_db)

        assert token is not None
        try:
            ctx = _current_project_context.get()
            assert ctx is not None
            assert ctx["id"] == "uuid-456"
            assert ctx["name"] == "gobby"
        finally:
            reset_project_context(token)

    def test_returns_none_when_not_found(self) -> None:
        """Returns None when project ref doesn't resolve."""
        mock_db = MagicMock()
        with patch("gobby.storage.projects.LocalProjectManager") as MockPM:
            MockPM.return_value.resolve_ref.return_value = None
            token = set_project_context_from_ref("nonexistent", mock_db)

        assert token is None

    def test_refactored_session_path_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """set_project_context_from_session still produces correct output after refactor."""
        monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
        mock_session = MagicMock()
        mock_session.project_id = "proj-abc"

        mock_project = MagicMock()
        mock_project.id = "proj-abc"
        mock_project.name = "my-project"
        mock_project.repo_path = None

        mock_session_manager = MagicMock()
        mock_session_manager.get.return_value = mock_session

        mock_db = MagicMock()
        with patch("gobby.storage.projects.LocalProjectManager") as MockPM:
            MockPM.return_value.get.return_value = mock_project
            token = set_project_context_from_session("sess-1", mock_session_manager, mock_db)

        assert token is not None
        try:
            ctx = _current_project_context.get()
            assert ctx is not None
            assert ctx["id"] == "proj-abc"
            assert ctx["name"] == "my-project"
            assert ctx["project_path"] is None
        finally:
            reset_project_context(token)
