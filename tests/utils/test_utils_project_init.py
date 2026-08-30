"""Tests for the project initialization utilities."""

import json
import os
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    CheckoutRootTakenError,
    CheckoutSentinelRejectedError,
    LocalProjectCheckoutManager,
    OverlayRegistrationRejectedError,
)
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    PERSONAL_PROJECT_ID,
    LocalProjectManager,
)
from gobby.utils.checkout_root import InvalidCheckoutRootError, MarkerMismatchError
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
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    patch_local_machine_id,
    write_project_marker,
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
    """Real initialize_project coverage complementary to the marker matrix."""

    def test_existing_project_file_registers_and_sanitizes_clone(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
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

        result = initialize_project(tmp_path, db=temp_db)

        project = LocalProjectManager(temp_db).get(project_id)
        assert result.already_existed is True
        assert project is not None
        assert project.repo_path in (None, "")
        assert _checkout_root(temp_db, machine_id, project_id) == str(tmp_path)
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
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_machine(temp_db, monkeypatch)
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

        result = initialize_project(worktree_root, db=temp_db)

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
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
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

        result = initialize_project(subdir, db=temp_db)

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
        assert _checkout_root(temp_db, machine_id, project_id) == str(tmp_path)

    def test_new_project_creation(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        with patch(
            "gobby.storage.hub.runtime.runtime_hub_database",
            return_value=nullcontext(temp_db),
        ) as open_db:
            result = initialize_project(tmp_path)

        open_db.assert_called_once_with(apply_migrations=False)
        project = LocalProjectManager(temp_db).get(result.project_id)
        assert project is not None
        assert project.name == tmp_path.name
        assert project.repo_path in (None, "")
        assert result.project_name == tmp_path.name
        assert result.already_existed is False
        assert _checkout_root(temp_db, machine_id, result.project_id) == str(tmp_path)

    def test_verification_commands_on_new_project(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_machine(temp_db, monkeypatch)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "src").mkdir()
        result = initialize_project(tmp_path, db=temp_db)
        assert result.verification is not None
        assert result.verification.unit_tests is not None
        assert result.verification.type_check is not None
        assert result.verification.lint is not None

    def test_no_verification_without_project_files(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_machine(temp_db, monkeypatch)
        result = initialize_project(tmp_path, db=temp_db)
        assert result.verification is None
        assert result.already_existed is False

    def test_github_url_is_stored(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_machine(temp_db, monkeypatch)
        result = initialize_project(
            tmp_path, github_url="https://github.com/custom/repo", db=temp_db
        )
        project = LocalProjectManager(temp_db).get(result.project_id)
        assert project is not None
        assert project.github_url == "https://github.com/custom/repo"

    def test_uses_cwd_when_none(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        monkeypatch.chdir(tmp_path)
        result = initialize_project(cwd=None, db=temp_db)
        assert result.project_id
        assert _checkout_root(temp_db, machine_id, result.project_id) == str(Path.cwd())


def _pin_machine(db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> str:
    machine_id = insert_isolated_machine(db)
    patch_local_machine_id(monkeypatch, machine_id)
    monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
    return machine_id


def _read_marker(root: Path) -> dict[str, Any]:
    loaded: object = json.loads((root / ".gobby" / "project.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _checkout_root(db: HubDatabase, machine_id: str, project_id: str) -> str | None:
    row = LocalProjectCheckoutManager(db).get(machine_id, project_id)
    return None if row is None else row.root_path


def _set_failpoint(monkeypatch: pytest.MonkeyPatch, name: str, hook: Any) -> None:
    import gobby.utils.project_init as project_init

    failpoints = project_init._INIT_FAILPOINTS
    monkeypatch.setitem(failpoints, name, hook)


def _clear_failpoints() -> None:
    import gobby.utils.project_init as project_init

    project_init._INIT_FAILPOINTS.clear()


def _name_attach_error() -> type[Exception]:
    from gobby.storage.projects import NameAttachRejectedError

    return NameAttachRejectedError


class TestMarkerAuthoritativeInit:
    """§ 2.1 marker-authoritative initialize_project / get_or_create matrix."""

    def test_no_marker_unused_name_registers_checkout(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        result = initialize_project(tmp_path, name="fresh-init", db=temp_db)
        marker = _read_marker(tmp_path)
        project = LocalProjectManager(temp_db).get(result.project_id)
        assert project is not None
        assert project.deleted_at is None
        assert project.name == "fresh-init"
        assert project.repo_path in (None, "")
        assert marker["id"] == result.project_id
        assert marker["name"] == "fresh-init"
        assert _checkout_root(temp_db, machine_id, result.project_id) == str(tmp_path)
        assert result.already_existed is False

    def test_valid_marker_registers_existing_project(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        project = LocalProjectManager(temp_db).create(name="marked-existing")
        write_project_marker(tmp_path, project_id=project.id, name="marked-existing")
        result = initialize_project(tmp_path, db=temp_db)
        assert result.project_id == project.id
        assert _checkout_root(temp_db, machine_id, project.id) == str(tmp_path)
        stored = LocalProjectManager(temp_db).get(project.id)
        assert stored is not None
        assert stored.repo_path in (None, "")

    def test_reject_existing_name_without_marker(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_machine(temp_db, monkeypatch)
        LocalProjectManager(temp_db).create(name="taken-name")
        with pytest.raises(_name_attach_error()):
            initialize_project(tmp_path, name="taken-name", db=temp_db)
        assert not (tmp_path / ".gobby" / "project.json").exists()

    def test_reject_soft_deleted_name_without_marker(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_machine(temp_db, monkeypatch)
        manager = LocalProjectManager(temp_db)
        deleted = manager.create(name="deleted-name")
        assert manager.soft_delete(deleted.id)
        with pytest.raises(_name_attach_error()):
            initialize_project(tmp_path, name="deleted-name", db=temp_db)
        leftover = manager.get(deleted.id)
        assert leftover is not None
        assert leftover.deleted_at is not None
        assert not (tmp_path / ".gobby" / "project.json").exists()

    def test_overlay_marker_refuses_registration(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        project = LocalProjectManager(temp_db).create(name="overlay-init")
        overlay = tmp_path / "wt"
        overlay.mkdir()
        write_project_marker(overlay, project_id=project.id, name="overlay-init")
        insert_overlay(
            temp_db,
            project_id=project.id,
            machine_id=machine_id,
            path=str(overlay),
            kind="worktree",
        )
        with pytest.raises(OverlayRegistrationRejectedError):
            initialize_project(overlay, db=temp_db)
        assert _checkout_root(temp_db, machine_id, project.id) is None

    def test_sentinel_marker_is_rejected(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        assert PERSONAL_PROJECT_ID in CHECKOUT_FREE_PROJECT_IDS
        write_project_marker(tmp_path, project_id=PERSONAL_PROJECT_ID, name="_personal")
        with pytest.raises(CheckoutSentinelRejectedError):
            initialize_project(tmp_path, db=temp_db)
        assert _checkout_root(temp_db, machine_id, PERSONAL_PROJECT_ID) is None

    def test_copied_marker_second_root_conflicts(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.storage.project_checkouts import CheckoutConflictError

        machine_id = _pin_machine(temp_db, monkeypatch)
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        result = initialize_project(first, name="copied-marker", db=temp_db)
        write_project_marker(second, project_id=result.project_id, name="copied-marker")
        with pytest.raises(CheckoutConflictError):
            initialize_project(second, db=temp_db)
        assert _checkout_root(temp_db, machine_id, result.project_id) == str(first)

    def test_relative_path_does_not_persist(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_machine(temp_db, monkeypatch)
        rel = Path("relative-init-root")
        monkeypatch.chdir(tmp_path)
        rel.mkdir()
        with pytest.raises(InvalidCheckoutRootError):
            initialize_project(rel, name="relative-init", db=temp_db)
        assert not (rel / ".gobby" / "project.json").exists()

    def test_get_or_create_does_not_name_attach_repo_path(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        manager = LocalProjectManager(temp_db)
        existing = manager.create(name="attach-me")
        with pytest.raises(_name_attach_error()):
            manager.get_or_create(name="attach-me", repo_path=str(tmp_path))
        stored = manager.get(existing.id)
        assert stored is not None
        assert stored.repo_path in (None, "")
        assert LocalProjectCheckoutManager(temp_db).get(machine_id, existing.id) is None
        assert not (tmp_path / ".gobby" / "project.json").exists()

    def test_marker_only_failpoint_retries_on_marker_id(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        hits = {"n": 0}

        def boom() -> None:
            hits["n"] += 1
            if hits["n"] == 1:
                raise RuntimeError("marker-only")

        _set_failpoint(monkeypatch, "after_marker_only", boom)
        with pytest.raises(RuntimeError, match="marker-only"):
            initialize_project(tmp_path, name="crash-resume", db=temp_db)
        marker = _read_marker(tmp_path)
        result = initialize_project(tmp_path, name="crash-resume", db=temp_db)
        assert result.project_id == marker["id"]
        assert _checkout_root(temp_db, machine_id, result.project_id) == str(tmp_path)
        rows = temp_db.fetchall(
            "SELECT id FROM projects WHERE name = %s AND deleted_at IS NULL",
            ("crash-resume",),
        )
        assert len(list(rows)) == 1

    def test_concurrent_same_root_one_winner(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        barrier = threading.Barrier(2)

        def worker() -> Any:
            barrier.wait(timeout=10)
            return initialize_project(tmp_path, name="same-root", db=temp_db)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: worker(), range(2)))
        assert first.project_id == second.project_id
        marker = _read_marker(tmp_path)
        assert marker["id"] == first.project_id
        assert _checkout_root(temp_db, machine_id, first.project_id) == str(tmp_path)
        missing = temp_db.fetchone(
            "SELECT id FROM projects WHERE id = %s",
            (marker["id"],),
        )
        assert missing is not None

    def test_publication_failpoints_never_expose_partial_marker(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        names = (
            "publish_before_temp_write",
            "publish_after_file_fsync",
            "publish_after_install",
            "publish_after_directory_fsync",
        )
        for failpoint in names:
            _clear_failpoints()
            root = tmp_path / failpoint
            root.mkdir()
            hits = {"n": 0}

            def boom(hits: dict[str, int] = hits, failpoint: str = failpoint) -> None:
                hits["n"] += 1
                if hits["n"] == 1:
                    raise RuntimeError(failpoint)

            _set_failpoint(monkeypatch, failpoint, boom)
            with pytest.raises(RuntimeError, match=failpoint):
                initialize_project(root, name=failpoint, db=temp_db)
            marker_path = root / ".gobby" / "project.json"
            if marker_path.exists():
                payload = json.loads(marker_path.read_text(encoding="utf-8"))
                assert payload.get("id")
                assert payload.get("name") == failpoint
            result = initialize_project(root, name=failpoint, db=temp_db)
            assert _checkout_root(temp_db, machine_id, result.project_id) == str(root)
            assert _read_marker(root)["id"] == result.project_id

    def test_concurrent_distinct_roots_same_name_loser_unlinks(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        right.mkdir()
        barrier = threading.Barrier(2)

        def worker(root: Path) -> Any:
            barrier.wait(timeout=10)
            return initialize_project(root, name="shared-name", db=temp_db)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, left), pool.submit(worker, right)]
            outcomes: list[Any] = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    outcomes.append(exc)
        wins = [item for item in outcomes if not isinstance(item, Exception)]
        losses = [item for item in outcomes if isinstance(item, Exception)]
        assert len(wins) == 1
        assert len(losses) == 1
        assert isinstance(losses[0], _name_attach_error())
        winner_root = left if (left / ".gobby" / "project.json").exists() else right
        loser_root = right if winner_root is left else left
        assert (winner_root / ".gobby" / "project.json").exists()
        if (loser_root / ".gobby" / "project.json").exists():
            assert _read_marker(loser_root)["id"] == wins[0].project_id
        assert _checkout_root(temp_db, machine_id, wins[0].project_id) == str(winner_root)

    def test_user_init_restores_soft_deleted_marker_rebind_preserves(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        manager = LocalProjectManager(temp_db)
        project = manager.create(name="restore-me")
        assert manager.soft_delete(project.id)
        write_project_marker(tmp_path, project_id=project.id, name="restore-me")
        result = initialize_project(tmp_path, db=temp_db)
        restored = manager.get(project.id)
        assert restored is not None
        assert restored.deleted_at is None
        assert result.project_id == project.id
        assert _checkout_root(temp_db, machine_id, project.id) == str(tmp_path)

        other = tmp_path / "rebind-root"
        other.mkdir()
        write_project_marker(other, project_id=project.id, name="restore-me")
        manager.soft_delete(project.id)
        LocalProjectCheckoutManager(temp_db).rebind(machine_id, project.id, str(other))
        still = manager.get(project.id)
        assert still is not None
        assert still.deleted_at is not None

    def test_name_uniqueness_failpoints_do_not_resurrect_orphan_marker(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        winner = tmp_path / "winner"
        winner.mkdir()
        initialize_project(winner, name="unique-fail", db=temp_db)
        for failpoint in (
            "name_reject_before_unlink",
            "name_reject_after_unlink",
            "name_reject_after_dir_fsync",
        ):
            _clear_failpoints()
            loser = tmp_path / failpoint
            loser.mkdir()
            hits = {"n": 0}

            def boom(hits: dict[str, int] = hits, failpoint: str = failpoint) -> None:
                hits["n"] += 1
                if hits["n"] == 1:
                    raise RuntimeError(failpoint)

            _set_failpoint(monkeypatch, failpoint, boom)
            with pytest.raises(RuntimeError, match=failpoint):
                initialize_project(loser, name="unique-fail", db=temp_db)
            replacement_id = str(uuid.uuid4())
            if failpoint == "name_reject_before_unlink":
                write_project_marker(loser, project_id=replacement_id, name="replacement")
                result = initialize_project(loser, db=temp_db)
                assert _read_marker(loser)["id"] == replacement_id
                assert result.project_id == replacement_id
            else:
                with pytest.raises(_name_attach_error()):
                    initialize_project(loser, name="unique-fail", db=temp_db)
                assert not (loser / ".gobby" / "project.json").exists()
            winner_id = _read_marker(winner)["id"]
            assert LocalProjectManager(temp_db).get(winner_id) is not None
            assert _checkout_root(temp_db, machine_id, winner_id) == str(winner)

    def test_same_root_different_explicit_names_one_payload(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        barrier = threading.Barrier(2)
        names = ["alpha-name", "beta-name"]

        def worker(name: str) -> Any:
            barrier.wait(timeout=10)
            try:
                return initialize_project(tmp_path, name=name, db=temp_db)
            except Exception as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, names))
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        assert successes
        marker = _read_marker(tmp_path)
        project = LocalProjectManager(temp_db).get(marker["id"])
        assert project is not None
        assert marker["name"] == project.name
        assert marker["id"] == project.id
        assert "created_at" in marker
        for success in successes:
            assert success.project_id == marker["id"]
            assert success.project_name == marker["name"]
        assert _checkout_root(temp_db, machine_id, marker["id"]) == str(tmp_path)

    def test_restore_blocked_when_name_active_on_other_uuid(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        manager = LocalProjectManager(temp_db)
        original = manager.create(name="name-clash")
        assert manager.soft_delete(original.id)
        active = manager.create(name="name-clash")
        write_project_marker(tmp_path, project_id=original.id, name="name-clash")
        original_marker = _read_marker(tmp_path)
        with pytest.raises(_name_attach_error()):
            initialize_project(tmp_path, db=temp_db)
        leftover = manager.get(original.id)
        assert leftover is not None
        assert leftover.deleted_at is not None
        assert manager.get(active.id) is not None
        assert _read_marker(tmp_path) == original_marker
        assert _checkout_root(temp_db, machine_id, original.id) is None

    def test_root_taken_unlinks_still_matching_marker(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        other = LocalProjectManager(temp_db).create(name="other-owner")
        LocalProjectCheckoutManager(temp_db).register(machine_id, other.id, str(tmp_path))
        with pytest.raises(CheckoutRootTakenError):
            initialize_project(tmp_path, name="new-at-taken-root", db=temp_db)
        assert not (tmp_path / ".gobby" / "project.json").exists() or (
            _read_marker(tmp_path)["id"] == other.id
        )
        assert LocalProjectManager(temp_db).get_by_name("new-at-taken-root") is None

    def test_root_taken_failpoints_and_retry(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        other = LocalProjectManager(temp_db).create(name="taken-owner")
        for failpoint in (
            "root_taken_before_unlink",
            "root_taken_after_unlink",
            "root_taken_after_dir_fsync",
        ):
            _clear_failpoints()
            root = tmp_path / failpoint
            root.mkdir()
            LocalProjectCheckoutManager(temp_db).rebind(machine_id, other.id, str(root))
            hits = {"n": 0}

            def boom(hits: dict[str, int] = hits, failpoint: str = failpoint) -> None:
                hits["n"] += 1
                if hits["n"] == 1:
                    raise RuntimeError(failpoint)

            _set_failpoint(monkeypatch, failpoint, boom)
            with pytest.raises(RuntimeError, match=failpoint):
                initialize_project(root, name=f"lost-{failpoint}", db=temp_db)
            replacement_id = str(uuid.uuid4())
            if failpoint == "root_taken_before_unlink":
                write_project_marker(root, project_id=replacement_id, name="kept")
            with pytest.raises(CheckoutRootTakenError):
                initialize_project(root, name=f"lost-{failpoint}", db=temp_db)
            if (root / ".gobby" / "project.json").exists():
                assert _read_marker(root)["id"] in {replacement_id, other.id}

    def test_overlay_recheck_unlinks_published_marker(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        overlay_project = LocalProjectManager(temp_db).create(name="overlay-owner")

        def insert_after_validate() -> None:
            insert_overlay(
                temp_db,
                project_id=overlay_project.id,
                machine_id=machine_id,
                path=str(tmp_path),
                kind="clone",
            )

        _set_failpoint(monkeypatch, "after_validate_before_register", insert_after_validate)
        with pytest.raises(OverlayRegistrationRejectedError):
            initialize_project(tmp_path, name="overlay-race", db=temp_db)
        assert not (tmp_path / ".gobby" / "project.json").exists()
        assert LocalProjectManager(temp_db).get_by_name("overlay-race") is None

    def test_overlay_recheck_failpoints_do_not_leave_orphan_marker(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        overlay_project = LocalProjectManager(temp_db).create(name="overlay-fp-owner")
        for failpoint in (
            "overlay_recheck_before_unlink",
            "overlay_recheck_after_unlink",
            "overlay_recheck_after_dir_fsync",
        ):
            _clear_failpoints()
            root = tmp_path / failpoint
            root.mkdir()

            def insert_overlay_row(root: Path = root) -> None:
                insert_overlay(
                    temp_db,
                    project_id=overlay_project.id,
                    machine_id=machine_id,
                    path=str(root),
                    kind="clone",
                )

            hits = {"n": 0}

            def boom(hits: dict[str, int] = hits, failpoint: str = failpoint) -> None:
                hits["n"] += 1
                if hits["n"] == 1:
                    raise RuntimeError(failpoint)

            _set_failpoint(monkeypatch, "after_validate_before_register", insert_overlay_row)
            _set_failpoint(monkeypatch, failpoint, boom)
            with pytest.raises(RuntimeError, match=failpoint):
                initialize_project(root, name=f"ov-{failpoint}", db=temp_db)
            with pytest.raises(OverlayRegistrationRejectedError):
                initialize_project(root, name=f"ov-{failpoint}", db=temp_db)
            assert not (root / ".gobby" / "project.json").exists() or (
                _read_marker(root)["id"] != overlay_project.id
            )

    def test_overlay_validate_after_publish_unlinks(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        overlay_project = LocalProjectManager(temp_db).create(name="overlay-validate-owner")

        def insert_after_publish() -> None:
            insert_overlay(
                temp_db,
                project_id=overlay_project.id,
                machine_id=machine_id,
                path=str(tmp_path),
                kind="worktree",
            )

        _set_failpoint(monkeypatch, "after_marker_only", insert_after_publish)
        with pytest.raises(OverlayRegistrationRejectedError):
            initialize_project(tmp_path, name="overlay-validate-race", db=temp_db)
        assert not (tmp_path / ".gobby" / "project.json").exists()

    def test_stale_marker_name_does_not_update_projects_name(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine_id = _pin_machine(temp_db, monkeypatch)
        manager = LocalProjectManager(temp_db)
        project = manager.create(name="canonical-name")
        write_project_marker(
            tmp_path,
            project_id=project.id,
            name="stale-name",
        )
        marker_path = tmp_path / ".gobby" / "project.json"
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload["created_at"] = "2024-01-02T00:00:00Z"
        payload["extra_field"] = "keep-me"
        marker_path.write_text(json.dumps(payload), encoding="utf-8")
        result = initialize_project(tmp_path, db=temp_db)
        stored = manager.get(project.id)
        assert stored is not None
        assert stored.name == "canonical-name"
        refreshed = _read_marker(tmp_path)
        assert refreshed["name"] == "canonical-name"
        assert refreshed["id"] == project.id
        assert refreshed["created_at"] == "2024-01-02T00:00:00Z"
        assert refreshed["extra_field"] == "keep-me"
        assert result.project_name == "canonical-name"
        assert _checkout_root(temp_db, machine_id, project.id) == str(tmp_path)
        manager.ensure_exists(project.id, "stale-name", str(tmp_path))
        after_ensure = manager.get(project.id)
        assert after_ensure is not None
        assert after_ensure.name == "canonical-name"

    def test_expected_id_refresh_refuses_replacement(
        self,
        tmp_path: Path,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gobby.utils.project_init import refresh_marker_expected_id

        _pin_machine(temp_db, monkeypatch)
        project = LocalProjectManager(temp_db).create(name="refresh-me")
        write_project_marker(tmp_path, project_id=project.id, name="old")
        marker_path = tmp_path / ".gobby" / "project.json"
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        payload["created_at"] = "2023-05-01T00:00:00Z"
        payload["note"] = "payload"
        marker_path.write_text(json.dumps(payload), encoding="utf-8")
        replacement_id = str(uuid.uuid4())

        def replace_marker() -> None:
            write_project_marker(tmp_path, project_id=replacement_id, name="replacement")

        _set_failpoint(monkeypatch, "refresh_after_temp_fsync", replace_marker)
        with pytest.raises(MarkerMismatchError):
            refresh_marker_expected_id(tmp_path, project.id, "refresh-me")
        assert _read_marker(tmp_path)["id"] == replacement_id
        _set_failpoint(monkeypatch, "refresh_after_temp_fsync", lambda: None)
        write_project_marker(tmp_path, project_id=project.id, name="old")
        payload = _read_marker(tmp_path)
        payload["created_at"] = "2023-05-01T00:00:00Z"
        payload["note"] = "payload"
        marker_path.write_text(json.dumps(payload), encoding="utf-8")
        hits = {"n": 0}

        def boom() -> None:
            hits["n"] += 1
            if hits["n"] == 1:
                raise RuntimeError("refresh_after_install")

        _set_failpoint(monkeypatch, "refresh_after_install", boom)
        with pytest.raises(RuntimeError, match="refresh_after_install"):
            refresh_marker_expected_id(tmp_path, project.id, "refresh-me")
        refresh_marker_expected_id(tmp_path, project.id, "refresh-me")
        refreshed = _read_marker(tmp_path)
        assert refreshed["id"] == project.id
        assert refreshed["name"] == "refresh-me"
        assert refreshed["created_at"] == "2023-05-01T00:00:00Z"
        assert refreshed["note"] == "payload"
