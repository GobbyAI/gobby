"""Tests for the project initialization utilities."""

import json
import os
import stat
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.utils.project_context import ensure_project_json_for_isolation, get_project_context
from gobby.utils.project_init import (
    InitResult,
    VerificationCommands,
    _find_frontend_dirs,
    _update_project_json_verification,
    _write_project_json,
    detect_verification_commands,
    initialize_project,
    update_project_json_fields,
)

pytestmark = pytest.mark.unit


class TestVerificationCommands:
    """Tests for the VerificationCommands dataclass."""

    def test_default_values(self) -> None:
        """Test that VerificationCommands has correct default values."""
        vc = VerificationCommands()
        assert vc.unit_tests is None
        assert vc.type_check is None
        assert vc.lint is None
        assert vc.format is None
        assert vc.integration is None
        assert vc.custom == {}

    def test_to_dict_empty(self) -> None:
        """Test to_dict returns empty dict when all values are None."""
        vc = VerificationCommands()
        assert vc.to_dict() == {}

    def test_to_dict_with_unit_tests(self) -> None:
        """Test to_dict includes unit_tests when set."""
        vc = VerificationCommands(unit_tests="pytest")
        result = vc.to_dict()
        assert result == {"unit_tests": "pytest"}

    def test_to_dict_with_type_check(self) -> None:
        """Test to_dict includes type_check when set."""
        vc = VerificationCommands(type_check="mypy .")
        result = vc.to_dict()
        assert result == {"type_check": "mypy ."}

    def test_to_dict_with_lint(self) -> None:
        """Test to_dict includes lint when set."""
        vc = VerificationCommands(lint="ruff check .")
        result = vc.to_dict()
        assert result == {"lint": "ruff check ."}

    def test_to_dict_with_format(self) -> None:
        """Test to_dict includes format when set."""
        vc = VerificationCommands(format="ruff format --check .")
        result = vc.to_dict()
        assert result == {"format": "ruff format --check ."}

    def test_to_dict_with_integration(self) -> None:
        """Test to_dict includes integration when set."""
        vc = VerificationCommands(integration="pytest tests/integration")
        result = vc.to_dict()
        assert result == {"integration": "pytest tests/integration"}

    def test_to_dict_with_custom(self) -> None:
        """Test to_dict includes custom when populated."""
        vc = VerificationCommands(custom={"build": "make build", "deploy": "make deploy"})
        result = vc.to_dict()
        assert result == {"custom": {"build": "make build", "deploy": "make deploy"}}

    def test_to_dict_with_all_values(self) -> None:
        """Test to_dict with all fields populated."""
        vc = VerificationCommands(
            unit_tests="pytest",
            type_check="mypy .",
            lint="ruff check .",
            integration="pytest tests/integration",
            custom={"build": "make build"},
        )
        result = vc.to_dict()
        assert result == {
            "unit_tests": "pytest",
            "type_check": "mypy .",
            "lint": "ruff check .",
            "integration": "pytest tests/integration",
            "custom": {"build": "make build"},
        }

    def test_to_dict_excludes_none_values(self) -> None:
        """Test that to_dict excludes None values but includes set ones."""
        vc = VerificationCommands(unit_tests="pytest", lint="ruff")
        result = vc.to_dict()
        assert "unit_tests" in result
        assert "lint" in result
        assert "type_check" not in result
        assert "integration" not in result
        assert "custom" not in result

    def test_to_dict_excludes_empty_custom(self) -> None:
        """Test that empty custom dict is excluded from to_dict output."""
        vc = VerificationCommands(unit_tests="pytest", custom={})
        result = vc.to_dict()
        assert "custom" not in result


class TestInitResult:
    """Tests for InitResult dataclass."""

    def test_init_result_creation(self) -> None:
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

    def test_init_result_already_existed(self) -> None:
        """Test InitResult with already_existed=True."""
        result = InitResult(
            project_id="existing-proj",
            project_name="existing",
            project_path="/path",
            created_at="2023-01-01T00:00:00Z",
            already_existed=True,
        )

        assert result.already_existed is True

    def test_init_result_with_verification(self) -> None:
        """Test InitResult with verification commands."""
        verification = VerificationCommands(unit_tests="pytest", lint="ruff")
        result = InitResult(
            project_id="proj-123",
            project_name="my-project",
            project_path="/path/to/project",
            created_at="2024-01-01T00:00:00Z",
            already_existed=False,
            verification=verification,
        )

        assert result.verification is not None
        assert result.verification.unit_tests == "pytest"
        assert result.verification.lint == "ruff"

    def test_init_result_verification_defaults_to_none(self) -> None:
        """Test that verification defaults to None."""
        result = InitResult(
            project_id="proj-123",
            project_name="my-project",
            project_path="/path/to/project",
            created_at="2024-01-01T00:00:00Z",
            already_existed=False,
        )

        assert result.verification is None


class TestDetectVerificationCommands:
    """Tests for detect_verification_commands function."""

    def test_no_project_files(self, tmp_path: Path) -> None:
        """Test detection when no recognized project files exist."""
        result = detect_verification_commands(tmp_path)

        assert result.unit_tests is None
        assert result.type_check is None
        assert result.lint is None
        assert result.integration is None
        assert result.custom == {}

    def test_python_project_with_tests_and_src(self, tmp_path: Path) -> None:
        """Test detection for Python project with tests/ and src/ directories."""
        # Create pyproject.toml
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")

        # Create tests and src directories
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests == "pytest tests/ -v"
        assert result.type_check == "mypy src/"
        assert result.lint == "ruff check src/"

    def test_python_project_with_tests_no_src(self, tmp_path: Path) -> None:
        """Test detection for Python project with tests/ but no src/ directory."""
        # Create pyproject.toml
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")

        # Create only tests directory
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests == "pytest tests/ -v"
        assert result.type_check == "mypy ."
        assert result.lint == "ruff check ."

    def test_python_project_with_src_no_tests(self, tmp_path: Path) -> None:
        """Test detection for Python project with src/ but no tests/ directory."""
        # Create pyproject.toml
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")

        # Create only src directory
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests is None
        assert result.type_check == "mypy src/"
        assert result.lint == "ruff check src/"

    def test_python_project_no_dirs(self, tmp_path: Path) -> None:
        """Test detection for Python project without tests/ or src/ directories."""
        # Create pyproject.toml
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests is None
        assert result.type_check == "mypy ."
        assert result.lint == "ruff check ."

    def test_nodejs_project_with_test_script(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with test script."""
        package_json = tmp_path / "package.json"
        package_json.write_text(json.dumps({"name": "test-project", "scripts": {"test": "jest"}}))

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests == "npm test"
        assert result.lint is None
        assert result.type_check is None

    def test_nodejs_project_with_lint_script(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with lint script."""
        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps({"name": "test-project", "scripts": {"lint": "eslint ."}})
        )

        result = detect_verification_commands(tmp_path)

        assert result.lint == "npm run lint"

    def test_nodejs_project_with_type_check_script(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with type-check script."""
        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps({"name": "test-project", "scripts": {"type-check": "tsc --noEmit"}})
        )

        result = detect_verification_commands(tmp_path)

        assert result.type_check == "npm run type-check"

    def test_nodejs_project_with_typecheck_script(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with typecheck script (no hyphen)."""
        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps({"name": "test-project", "scripts": {"typecheck": "tsc --noEmit"}})
        )

        result = detect_verification_commands(tmp_path)

        assert result.type_check == "npm run typecheck"

    def test_nodejs_project_with_types_script(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with types script."""
        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps({"name": "test-project", "scripts": {"types": "tsc --noEmit"}})
        )

        result = detect_verification_commands(tmp_path)

        assert result.type_check == "npm run types"

    def test_nodejs_project_with_tsc_script(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with tsc script."""
        package_json = tmp_path / "package.json"
        package_json.write_text(json.dumps({"name": "test-project", "scripts": {"tsc": "tsc"}}))

        result = detect_verification_commands(tmp_path)

        assert result.type_check == "npm run tsc"

    def test_nodejs_project_with_all_scripts(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with all relevant scripts."""
        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps(
                {
                    "name": "test-project",
                    "scripts": {"test": "jest", "lint": "eslint .", "type-check": "tsc --noEmit"},
                }
            )
        )

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests == "npm test"
        assert result.lint == "npm run lint"
        assert result.type_check == "npm run type-check"

    def test_nodejs_project_no_scripts(self, tmp_path: Path) -> None:
        """Test detection for Node.js project without scripts."""
        package_json = tmp_path / "package.json"
        package_json.write_text(json.dumps({"name": "test-project"}))

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests is None
        assert result.lint is None
        assert result.type_check is None

    def test_nodejs_project_empty_scripts(self, tmp_path: Path) -> None:
        """Test detection for Node.js project with empty scripts object."""
        package_json = tmp_path / "package.json"
        package_json.write_text(json.dumps({"name": "test-project", "scripts": {}}))

        result = detect_verification_commands(tmp_path)

        assert result.unit_tests is None
        assert result.lint is None
        assert result.type_check is None

    def test_nodejs_project_invalid_json(self, tmp_path: Path) -> None:
        """Test detection when package.json contains invalid JSON."""
        package_json = tmp_path / "package.json"
        package_json.write_text("{ invalid json }")

        result = detect_verification_commands(tmp_path)

        # Should return empty verification commands without crashing
        assert result.unit_tests is None
        assert result.lint is None
        assert result.type_check is None

    def test_nodejs_project_type_check_script_priority(self, tmp_path: Path) -> None:
        """Test that type-check script has priority over other type check scripts."""
        package_json = tmp_path / "package.json"
        package_json.write_text(
            json.dumps(
                {
                    "name": "test-project",
                    "scripts": {
                        "tsc": "tsc",
                        "types": "tsc --noEmit",
                        "typecheck": "tsc --noEmit --watch",
                        "type-check": "tsc --noEmit --strict",
                    },
                }
            )
        )

        result = detect_verification_commands(tmp_path)

        # type-check should be selected first due to iteration order
        assert result.type_check == "npm run type-check"

    def test_python_project_tests_is_file_not_dir(self, tmp_path: Path) -> None:
        """Test that tests file (not directory) doesn't trigger test command."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")

        # Create tests as a file, not a directory
        tests_file = tmp_path / "tests"
        tests_file.write_text("# This is a file, not a directory")

        result = detect_verification_commands(tmp_path)

        # Should not detect tests since it's a file
        assert result.unit_tests is None

    def test_python_project_src_is_file_not_dir(self, tmp_path: Path) -> None:
        """Test that src file (not directory) triggers fallback commands."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")

        # Create src as a file, not a directory
        src_file = tmp_path / "src"
        src_file.write_text("# This is a file, not a directory")

        result = detect_verification_commands(tmp_path)

        # Should use fallback commands since src is a file
        assert result.type_check == "mypy ."
        assert result.lint == "ruff check ."


class TestFindFrontendDirs:
    """Tests for _find_frontend_dirs helper."""

    def test_no_package_json(self, tmp_path: Path) -> None:
        """No package.json anywhere."""
        assert _find_frontend_dirs(tmp_path) == []

    def test_root_package_json(self, tmp_path: Path) -> None:
        """package.json at root only."""
        (tmp_path / "package.json").write_text("{}")
        result = _find_frontend_dirs(tmp_path)
        assert len(result) == 1
        assert result[0][1] == "."

    def test_web_subdir(self, tmp_path: Path) -> None:
        """package.json in web/ subdirectory."""
        web = tmp_path / "web"
        web.mkdir()
        (web / "package.json").write_text("{}")
        result = _find_frontend_dirs(tmp_path)
        assert len(result) == 1
        assert result[0][1] == "web"

    def test_frontend_subdir(self, tmp_path: Path) -> None:
        """package.json in frontend/ subdirectory."""
        fe = tmp_path / "frontend"
        fe.mkdir()
        (fe / "package.json").write_text("{}")
        result = _find_frontend_dirs(tmp_path)
        assert len(result) == 1
        assert result[0][1] == "frontend"

    def test_root_and_web(self, tmp_path: Path) -> None:
        """Both root and web/ have package.json."""
        (tmp_path / "package.json").write_text("{}")
        web = tmp_path / "web"
        web.mkdir()
        (web / "package.json").write_text("{}")
        result = _find_frontend_dirs(tmp_path)
        assert len(result) == 2
        assert result[0][1] == "."
        assert result[1][1] == "web"


class TestDetectVerificationMultiLanguage:
    """Tests for multi-language detection (Python + frontend subdirectory)."""

    def test_python_with_web_frontend(self, tmp_path: Path) -> None:
        """Python project with web/ frontend detects both."""
        # Python
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "src").mkdir()

        # Frontend in web/
        web = tmp_path / "web"
        web.mkdir()
        (web / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "test": "vitest run",
                        "type-check": "tsc --noEmit",
                        "lint": "eslint .",
                    }
                }
            )
        )

        result = detect_verification_commands(tmp_path)

        # Python claims primary slots
        assert result.unit_tests == "pytest tests/ -v"
        assert result.type_check == "mypy src/"
        assert result.lint == "ruff check src/"
        assert result.format == "ruff format --check src/"

        # Frontend goes to custom with cd prefix
        assert result.custom["frontend_tests"] == "cd web && npm test"
        assert result.custom["ts_check"] == "cd web && npm run type-check"
        assert result.custom["frontend_lint"] == "cd web && npm run lint"

    def test_python_detects_format(self, tmp_path: Path) -> None:
        """Python project with src/ detects ruff format."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        (tmp_path / "src").mkdir()

        result = detect_verification_commands(tmp_path)
        assert result.format == "ruff format --check src/"

    def test_python_no_src_detects_format_fallback(self, tmp_path: Path) -> None:
        """Python project without src/ uses fallback format command."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")

        result = detect_verification_commands(tmp_path)
        assert result.format == "ruff format --check ."

    def test_standalone_web_frontend(self, tmp_path: Path) -> None:
        """Only web/ frontend, no Python — frontend gets primary slots."""
        web = tmp_path / "web"
        web.mkdir()
        (web / "package.json").write_text(
            json.dumps(
                {"scripts": {"test": "vitest", "lint": "eslint .", "type-check": "tsc --noEmit"}}
            )
        )

        result = detect_verification_commands(tmp_path)

        # No Python, so frontend gets primary slots with cd prefix
        assert result.unit_tests == "cd web && npm test"
        assert result.lint == "cd web && npm run lint"
        assert result.type_check == "cd web && npm run type-check"

    def test_root_nodejs_no_subdir(self, tmp_path: Path) -> None:
        """Root package.json without subdirectory uses no cd prefix."""
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))

        result = detect_verification_commands(tmp_path)
        assert result.unit_tests == "npm test"


class TestUpdateProjectJsonVerification:
    """Tests for _update_project_json_verification."""

    def test_updates_verification_in_existing_project(self, tmp_path: Path) -> None:
        """Re-init updates verification commands in project.json."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_file = gobby_dir / "project.json"
        project_file.write_text(
            json.dumps(
                {
                    "id": "proj-123",
                    "name": "test",
                    "created_at": "2024-01-01",
                    "verification": {"unit_tests": "old command"},
                    "hooks": {"pre-commit": {"run": ["lint"]}},
                }
            )
        )

        verification = VerificationCommands(
            unit_tests="GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v",
            lint="uv run ruff check src/",
            format="uv run ruff format --check src/",
        )
        _update_project_json_verification(tmp_path, verification)

        content = json.loads(project_file.read_text())
        assert content["verification"]["unit_tests"] == (
            "GOBBY_TEST_PROTECT=1 uv run pytest tests/ -v"
        )
        assert content["verification"]["lint"] == "uv run ruff check src/"
        assert content["verification"]["format"] == "uv run ruff format --check src/"
        # Hooks preserved
        assert content["hooks"]["pre-commit"]["run"] == ["lint"]
        # Other fields preserved
        assert content["id"] == "proj-123"

    def test_preserves_manual_custom_entries(self, tmp_path: Path) -> None:
        """Manual custom entries not in detection are preserved."""
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        project_file = gobby_dir / "project.json"
        project_file.write_text(
            json.dumps(
                {
                    "id": "proj-123",
                    "name": "test",
                    "created_at": "2024-01-01",
                    "verification": {
                        "unit_tests": "old",
                        "custom": {"manual_check": "make verify"},
                    },
                }
            )
        )

        verification = VerificationCommands(
            unit_tests="pytest",
            custom={"frontend_tests": "cd web && npm test"},
        )
        _update_project_json_verification(tmp_path, verification)

        content = json.loads(project_file.read_text())
        custom = content["verification"]["custom"]
        assert custom["manual_check"] == "make verify"
        assert custom["frontend_tests"] == "cd web && npm test"

    def test_noop_when_no_project_json(self, tmp_path: Path) -> None:
        """Does nothing if project.json doesn't exist."""
        verification = VerificationCommands(unit_tests="pytest")
        result = _update_project_json_verification(tmp_path, verification)
        assert result is None
        assert not (tmp_path / ".gobby" / "project.json").exists()


class TestWriteProjectJson:
    """Tests for _write_project_json function."""

    def test_creates_gobby_dir(self, tmp_path: Path) -> None:
        """Test that .gobby directory is created if it doesn't exist."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        _write_project_json(cwd, "proj-id", "test-project", "2024-01-01")

        gobby_dir = cwd / ".gobby"
        assert gobby_dir.exists()
        assert gobby_dir.is_dir()

    def test_writes_project_json(self, tmp_path: Path) -> None:
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

    def test_update_omits_nonportable_project_fields(self, tmp_path: Path) -> None:
        """Committed project metadata excludes machine-local bindings."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        _write_project_json(cwd, "proj-123", "my-project", "2024-06-15T12:00:00Z")
        update_project_json_fields(
            cwd,
            linear_team_id="team-1",
            linear_project_id="lin-proj",
            parent_project_id="parent-id",
            parent_project_path="/machine/local/path",
            hooks={"mode": "default"},
        )

        content = json.loads((cwd / ".gobby" / "project.json").read_text())
        assert content["hooks"] == {"mode": "default"}
        assert "linear_team_id" not in content
        assert "linear_project_id" not in content
        assert "parent_project_id" not in content
        assert "parent_project_path" not in content

    def test_overwrites_existing_project_json(self, tmp_path: Path) -> None:
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

    def test_destination_stays_complete_until_atomic_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "project"
        project_file = cwd / ".gobby" / "project.json"
        project_file.parent.mkdir(parents=True)
        original = {"id": "old-id", "name": "old-name"}
        project_file.write_text(json.dumps(original), encoding="utf-8")
        real_replace = os.replace

        def assert_complete_then_replace(src: str, dst: str | Path) -> None:
            assert json.loads(Path(dst).read_text(encoding="utf-8")) == original
            assert json.loads(Path(src).read_text(encoding="utf-8")) == {
                "id": "new-id",
                "name": "new-name",
                "created_at": "2024-01-01",
            }
            real_replace(src, dst)

        monkeypatch.setattr("gobby.utils.project_init.os.replace", assert_complete_then_replace)

        _write_project_json(cwd, "new-id", "new-name", "2024-01-01")

        assert json.loads(project_file.read_text(encoding="utf-8"))["id"] == "new-id"

    def test_atomic_write_preserves_project_file_mode(self, tmp_path: Path) -> None:
        cwd = tmp_path / "project"
        cwd.mkdir()
        _write_project_json(cwd, "proj-123", "my-project", "2024-01-01")
        project_file = cwd / ".gobby" / "project.json"
        project_file.chmod(0o640)

        update_project_json_fields(cwd, hooks={"mode": "default"})

        assert stat.S_IMODE(project_file.stat().st_mode) == 0o640

    def test_handles_existing_gobby_dir(self, tmp_path: Path) -> None:
        """Test that existing .gobby directory is handled correctly."""
        cwd = tmp_path / "project"
        cwd.mkdir()
        gobby_dir = cwd / ".gobby"
        gobby_dir.mkdir()

        # Should not raise even if dir exists
        _write_project_json(cwd, "proj-id", "name", "2024-01-01")

        assert (gobby_dir / "project.json").exists()

    def test_writes_verification_commands(self, tmp_path: Path) -> None:
        """Test that verification commands are included in project.json."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        verification = VerificationCommands(
            unit_tests="pytest",
            type_check="mypy .",
            lint="ruff check .",
        )

        _write_project_json(cwd, "proj-123", "my-project", "2024-01-01", verification)

        project_file = cwd / ".gobby" / "project.json"
        content = json.loads(project_file.read_text())

        assert "verification" in content
        assert content["verification"]["unit_tests"] == "pytest"
        assert content["verification"]["type_check"] == "mypy ."
        assert content["verification"]["lint"] == "ruff check ."

    def test_omits_empty_verification_commands(self, tmp_path: Path) -> None:
        """Test that empty verification commands are not included."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        verification = VerificationCommands()  # All None

        _write_project_json(cwd, "proj-123", "my-project", "2024-01-01", verification)

        project_file = cwd / ".gobby" / "project.json"
        content = json.loads(project_file.read_text())

        assert "verification" not in content

    def test_writes_verification_with_custom_commands(self, tmp_path: Path) -> None:
        """Test that custom verification commands are included."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        verification = VerificationCommands(custom={"build": "make build", "deploy": "make deploy"})

        _write_project_json(cwd, "proj-123", "my-project", "2024-01-01", verification)

        project_file = cwd / ".gobby" / "project.json"
        content = json.loads(project_file.read_text())

        assert "verification" in content
        assert content["verification"]["custom"]["build"] == "make build"
        assert content["verification"]["custom"]["deploy"] == "make deploy"

    def test_writes_json_with_proper_formatting(self, tmp_path: Path) -> None:
        """Test that project.json is written with proper indentation."""
        cwd = tmp_path / "project"
        cwd.mkdir()

        _write_project_json(cwd, "proj-123", "my-project", "2024-01-01")

        project_file = cwd / ".gobby" / "project.json"
        content = project_file.read_text()

        # Should have indentation (not a single line)
        assert "\n" in content
        # Should be parseable
        parsed = json.loads(content)
        assert parsed["id"] == "proj-123"


class TestInitializeProject:
    """Tests for initialize_project function."""

    def test_already_initialized_returns_existing(self, tmp_path: Path) -> None:
        """Test that already initialized project returns existing info."""
        # Patch at the source modules where they are imported from
        with (
            patch("gobby.utils.project_context.get_project_context") as mock_ctx,
            patch("gobby.storage.projects.LocalProjectManager") as manager_cls,
        ):
            mock_ctx.return_value = {
                "id": "existing-id",
                "name": "existing-name",
                "project_path": str(tmp_path),
                "created_at": "2024-01-01",
            }

            result = initialize_project(tmp_path, db=MagicMock())

            assert result.project_id == "existing-id"
            assert result.project_name == "existing-name"
            assert result.already_existed is True
            manager_cls.return_value.ensure_exists.assert_called_once_with(
                "existing-id", "existing-name", str(tmp_path.resolve())
            )

    def test_existing_project_file_registers_and_sanitizes_clone(
        self, tmp_path: Path, hub_db: HubDatabase
    ) -> None:
        project_id = str(uuid.uuid4())
        project_file = tmp_path / ".gobby" / "project.json"
        project_file.parent.mkdir()
        project_file.write_text(
            json.dumps(
                {
                    "id": project_id,
                    "name": "fresh-clone",
                    "created_at": "2024-01-01T00:00:00Z",
                    "linear_team_id": "team-1",
                    "linear_project_id": "linear-project-1",
                    "parent_project_id": "parent-id",
                    "parent_project_path": "/original/machine/path",
                }
            ),
            encoding="utf-8",
        )

        result = initialize_project(tmp_path, db=hub_db)

        project = LocalProjectManager(hub_db).get(project_id)
        assert result.already_existed is True
        assert project is not None
        assert project.repo_path == str(tmp_path.resolve())
        content = json.loads(project_file.read_text(encoding="utf-8"))
        assert set(content).isdisjoint(
            {
                "linear_team_id",
                "linear_project_id",
                "parent_project_id",
                "parent_project_path",
            }
        )

    def test_reinit_preserves_generated_isolation_parent_metadata(
        self, tmp_path: Path, hub_db: HubDatabase
    ) -> None:
        parent_root = tmp_path / "parent"
        worktree_root = tmp_path / "worktree"
        parent_root.mkdir()
        worktree_root.mkdir()
        project_id = str(uuid.uuid4())
        _write_project_json(
            parent_root,
            project_id,
            "parent-project",
            "2024-01-01T00:00:00Z",
        )
        ensure_project_json_for_isolation(parent_root, worktree_root)

        result = initialize_project(worktree_root, db=hub_db)

        project_file = worktree_root / ".gobby" / "project.json"
        content = json.loads(project_file.read_text(encoding="utf-8"))
        assert result.already_existed is True
        assert "parent_project_path" not in content
        marker = json.loads(
            (worktree_root / ".gobby" / "isolation.json").read_text(encoding="utf-8")
        )
        assert marker["parent_project_path"] == str(parent_root.resolve())
        assert marker["parent_project_id"] == project_id

    def test_reinit_from_subdirectory_refreshes_project_root(
        self, tmp_path: Path, hub_db: HubDatabase
    ) -> None:
        """Re-init refreshes the discovered project root instead of the requested cwd."""
        project_id = str(uuid.uuid4())
        project_file = tmp_path / ".gobby" / "project.json"
        project_file.parent.mkdir()
        project_file.write_text(
            json.dumps(
                {
                    "id": project_id,
                    "name": "existing-name",
                    "created_at": "2024-01-01",
                    "verification": {"unit_tests": "old command"},
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()

        subdir = tmp_path / "packages" / "api"
        subdir.mkdir(parents=True)
        (subdir / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        (subdir / "tests").mkdir()

        result = initialize_project(subdir, db=hub_db)

        root_data = json.loads(project_file.read_text(encoding="utf-8"))
        assert result.project_id == project_id
        assert not (subdir / ".gobby" / "project.json").exists()
        assert root_data["id"] == project_id
        assert root_data["verification"] != {"unit_tests": "old command"}
        assert result.verification is not None
        assert result.verification.to_dict() == root_data["verification"]
        assert get_project_context(subdir) == {
            **root_data,
            "project_path": str(tmp_path),
        }

    def test_already_initialized_with_empty_id(self, tmp_path: Path) -> None:
        """Test that project with empty id is treated as uninitialized."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {
                "id": "",  # Empty id
                "name": "test",
            }

            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "new-proj-id"
                        mock_project.name = tmp_path.name
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(tmp_path)

                        # Should create new project since id was empty
                        assert result.already_existed is False
                        assert result.project_id == "new-proj-id"

    def test_new_project_creation(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh initialization creates the project in the isolated database."""
        monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
        with patch(
            "gobby.storage.hub.runtime.runtime_hub_database", return_value=nullcontext(temp_db)
        ) as open_db:
            result = initialize_project(tmp_path)

        open_db.assert_called_once_with(apply_migrations=False)

        project = LocalProjectManager(temp_db).get(result.project_id)
        assert project is not None
        assert project.name == tmp_path.name
        assert project.repo_path == str(tmp_path)
        assert result.project_name == tmp_path.name
        assert result.already_existed is False

    def test_uses_provided_name(self, tmp_path: Path) -> None:
        """Test that provided name overrides directory name."""
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "id"
                        mock_project.name = "custom-name"
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(tmp_path, name="custom-name")

                        call_kwargs = mock_pm_instance.create.call_args
                        assert call_kwargs.kwargs["name"] == "custom-name"
                        assert result.project_name == "custom-name"

    def test_uses_provided_github_url(self, tmp_path: Path) -> None:
        """Test that provided github_url is used."""
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value="https://auto-detected.com"):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "id"
                        mock_project.name = "name"
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(
                            tmp_path, github_url="https://github.com/custom/repo"
                        )

                        call_kwargs = mock_pm_instance.create.call_args
                        assert call_kwargs.kwargs["github_url"] == "https://github.com/custom/repo"
                        assert result.project_id == "id"

    def test_auto_detects_github_url(self, tmp_path: Path) -> None:
        """Test that github URL is auto-detected from git remote."""
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch(
                "gobby.utils.git.get_github_url", return_value="https://github.com/detected/repo"
            ):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "id"
                        mock_project.name = "name"
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(tmp_path)

                        call_kwargs = mock_pm_instance.create.call_args
                        assert (
                            call_kwargs.kwargs["github_url"] == "https://github.com/detected/repo"
                        )
                        assert result.project_id == "id"

    def test_existing_db_project_no_local_json(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Initialization adopts a same-name database project and writes local state."""
        manager = LocalProjectManager(temp_db)
        existing = manager.create(name=tmp_path.name)
        monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
        monkeypatch.setattr(
            "gobby.storage.hub.runtime.runtime_hub_database",
            lambda *, apply_migrations: nullcontext(temp_db),
        )

        result = initialize_project(tmp_path)

        adopted = manager.get(existing.id)
        assert adopted is not None
        assert adopted.repo_path == str(tmp_path)
        assert result.project_id == existing.id
        assert result.already_existed is True
        project_data = json.loads(
            (tmp_path / ".gobby" / "project.json").read_text(encoding="utf-8")
        )
        assert project_data["id"] == existing.id

    def test_rejects_same_name_project_from_different_repo(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        other_repo = tmp_path.parent / "other" / tmp_path.name
        other_repo.mkdir(parents=True)
        manager = LocalProjectManager(temp_db)
        existing = manager.create(name=tmp_path.name, repo_path=str(other_repo))
        monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
        monkeypatch.setattr(
            "gobby.storage.hub.runtime.runtime_hub_database",
            lambda *, apply_migrations: nullcontext(temp_db),
        )

        with pytest.raises(ValueError, match=r"different repository.*--name"):
            initialize_project(tmp_path)

        assert not (tmp_path / ".gobby" / "project.json").exists()
        unchanged = manager.get(existing.id)
        assert unchanged is not None
        assert unchanged.repo_path == str(other_repo)

    def test_restores_soft_deleted_project(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = LocalProjectManager(temp_db)
        deleted = manager.create(name=tmp_path.name, repo_path=str(tmp_path))
        assert manager.soft_delete(deleted.id)
        monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
        monkeypatch.setattr(
            "gobby.storage.hub.runtime.runtime_hub_database",
            lambda *, apply_migrations: nullcontext(temp_db),
        )

        result = initialize_project(tmp_path)

        restored = manager.get(deleted.id)
        assert restored is not None
        assert restored.deleted_at is None
        assert result.project_id == deleted.id
        assert result.already_existed is True

    def test_adopts_project_created_concurrently(self, tmp_path: Path) -> None:
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        winner = MagicMock()
                        winner.id = "winner-project-id"
                        winner.name = tmp_path.name
                        winner.repo_path = str(tmp_path)
                        winner.created_at = datetime(2024, 6, 15, tzinfo=UTC)

                        manager = mock_pm_cls.return_value
                        manager.get_by_name.side_effect = [None, winner]
                        manager.create.side_effect = UniqueViolation("duplicate project name")

                        result = initialize_project(tmp_path)

                        assert result.project_id == winner.id
                        assert result.already_existed is True
                        assert manager.get_by_name.call_count == 2

    def test_uses_cwd_when_none(self, hub_db: HubDatabase) -> None:
        """Test that current working directory is used when cwd is None."""
        project_id = str(uuid.uuid4())
        mock_project_context = {
            "id": project_id,
            "name": "name",
            "project_path": "/test",
            "created_at": "2024",
        }

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value=mock_project_context,
        ):
            with patch("pathlib.Path.cwd") as mock_cwd:
                mock_cwd.return_value = Path("/some/path")

                result = initialize_project(cwd=None, db=hub_db)

                # Should use cwd
                assert result.project_id == project_id

    def test_project_context_none_id(self, tmp_path: Path) -> None:
        """Test when project context exists but id is None."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {
                "id": None,  # None id
                "name": "test",
            }

            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "new-proj-id"
                        mock_project.name = tmp_path.name
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(tmp_path)

                        # Should create new project since id was None
                        assert result.already_existed is False
                        assert result.project_id == "new-proj-id"

    def test_new_project_with_verification_commands(self, tmp_path: Path) -> None:
        """Test that new project creation includes verification commands."""
        # Create pyproject.toml to trigger verification detection
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")

        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "new-proj-id"
                        mock_project.name = tmp_path.name
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(tmp_path)

                        assert result.verification is not None
                        assert result.verification.unit_tests == "pytest tests/ -v"
                        assert result.verification.type_check == "mypy src/"
                        assert result.verification.lint == "ruff check src/"

    def test_existing_db_project_includes_verification(self, tmp_path: Path) -> None:
        """Test that existing DB project includes verification commands when synced."""
        # Create pyproject.toml for verification detection
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'test'\n")
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_existing = MagicMock()
                        mock_existing.id = "db-proj-id"
                        mock_existing.name = tmp_path.name
                        mock_existing.repo_path = str(tmp_path)
                        mock_existing.created_at = datetime(2023, 1, 1, tzinfo=UTC)
                        mock_existing.deleted_at = None

                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = mock_existing

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(tmp_path)

                        # Should include verification
                        assert result.verification is not None
                        assert result.verification.type_check == "mypy src/"

    def test_new_project_without_verification_commands(self, tmp_path: Path) -> None:
        """Test that new project without recognizable structure has no verification."""
        # No pyproject.toml or package.json

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "new-proj-id"
                        mock_project.name = tmp_path.name
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(tmp_path)

                        # No verification since no recognizable project type
                        assert result.verification is None
                        assert result.project_id == "new-proj-id"

    def test_path_resolution(self, tmp_path: Path) -> None:
        """Test that path is properly resolved."""
        # Create a subdirectory
        subdir = tmp_path / "subdir" / "project"
        subdir.mkdir(parents=True)

        with (
            patch("gobby.utils.project_context.get_project_context") as mock_ctx,
            patch.object(LocalProjectManager, "ensure_exists"),
        ):
            mock_ctx.return_value = {
                "id": "existing-id",
                "name": "existing-name",
                "project_path": str(subdir.resolve()),
                "created_at": "2024-01-01",
            }

            result = initialize_project(subdir, db=MagicMock())

            assert result.project_path == str(subdir.resolve())

    def test_directory_name_used_as_project_name(self, tmp_path: Path) -> None:
        """Test that directory name is used when no name provided."""
        project_dir = tmp_path / "my-awesome-project"
        project_dir.mkdir()

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with patch("gobby.utils.git.get_github_url", return_value=None):
                with patch("gobby.storage.hub.runtime.runtime_hub_database"):
                    with patch("gobby.storage.projects.LocalProjectManager") as mock_pm_cls:
                        mock_pm_instance = MagicMock()
                        mock_pm_instance.get_by_name.return_value = None

                        mock_project = MagicMock()
                        mock_project.id = "id"
                        mock_project.name = "my-awesome-project"
                        mock_project.created_at = datetime(2024, 1, 1, tzinfo=UTC)
                        mock_pm_instance.create.return_value = mock_project

                        mock_pm_cls.return_value = mock_pm_instance

                        result = initialize_project(project_dir)

                        call_kwargs = mock_pm_instance.create.call_args
                        assert call_kwargs.kwargs["name"] == "my-awesome-project"
                        assert result.project_name == "my-awesome-project"

    def test_already_initialized_returns_correct_project_path(self, tmp_path: Path) -> None:
        """Test that project_path from context is used when already initialized."""
        with (
            patch("gobby.utils.project_context.get_project_context") as mock_ctx,
            patch.object(LocalProjectManager, "ensure_exists"),
        ):
            mock_ctx.return_value = {
                "id": "existing-id",
                "name": "existing-name",
                "project_path": "/original/path",
                "created_at": "2024-01-01",
            }

            result = initialize_project(tmp_path, db=MagicMock())

            # Should use project_path from context
            assert result.project_path == "/original/path"

    def test_already_initialized_with_missing_project_path(self, tmp_path: Path) -> None:
        """Test when project context exists but project_path is missing."""
        with (
            patch("gobby.utils.project_context.get_project_context") as mock_ctx,
            patch.object(LocalProjectManager, "ensure_exists"),
        ):
            mock_ctx.return_value = {
                "id": "existing-id",
                "name": "existing-name",
                # No project_path
                "created_at": "2024-01-01",
            }

            result = initialize_project(tmp_path, db=MagicMock())

            # Should fall back to cwd
            assert result.project_path == str(tmp_path.resolve())

    def test_already_initialized_with_missing_created_at(self, tmp_path: Path) -> None:
        """Test when project context exists but created_at is missing."""
        with (
            patch("gobby.utils.project_context.get_project_context") as mock_ctx,
            patch.object(LocalProjectManager, "ensure_exists"),
        ):
            mock_ctx.return_value = {
                "id": "existing-id",
                "name": "existing-name",
                "project_path": str(tmp_path),
                # No created_at
            }

            result = initialize_project(tmp_path, db=MagicMock())

            # Should use empty string as default
            assert result.created_at == ""

    def test_already_initialized_with_missing_name(self, tmp_path: Path) -> None:
        """Test when project context exists but name is missing."""
        with (
            patch("gobby.utils.project_context.get_project_context") as mock_ctx,
            patch.object(LocalProjectManager, "ensure_exists"),
        ):
            mock_ctx.return_value = {
                "id": "existing-id",
                # No name
                "project_path": str(tmp_path),
                "created_at": "2024-01-01",
            }

            result = initialize_project(tmp_path, db=MagicMock())

            # Should use empty string as default
            assert result.project_name == ""
