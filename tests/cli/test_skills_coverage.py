"""Tests for cli/skills.py — targeting uncovered lines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli.skills import skills

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_skill(**overrides: Any) -> MagicMock:
    defaults = {
        "id": "skill-123",
        "name": "test-skill",
        "description": "A test skill for testing",
        "version": "1.0.0",
        "license": "MIT",
        "enabled": True,
        "source_type": "template",
        "source_path": "/path/to/skill",
        "content": "# Test skill content",
        "metadata": {"author": "tester", "skillport": {"category": "git", "tags": ["test"]}},
        "compatibility": None,
    }
    defaults.update(overrides)
    skill = MagicMock()
    for k, v in defaults.items():
        setattr(skill, k, v)
    return skill


def _make_config_obj() -> dict[str, Any]:
    from gobby.config.app import DaemonConfig

    config = MagicMock(spec=DaemonConfig)
    config.daemon_port = 60888
    return {"config": config}


# ---------------------------------------------------------------------------
# skills list
# ---------------------------------------------------------------------------
class TestSkillsList:
    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_empty(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.list_skills.return_value = []
        result = runner.invoke(skills, ["list"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 0
        assert "No skills found" in result.output

    @patch("gobby.cli.skills.get_skill_category", return_value="git")
    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_with_skills(
        self, mock_storage_fn: MagicMock, _cat: MagicMock, runner: CliRunner
    ) -> None:
        skill = _mock_skill()
        mock_storage_fn.return_value.list_skills.return_value = [skill]
        result = runner.invoke(skills, ["list"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 0
        assert "test-skill" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_disabled_skill(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        skill = _mock_skill(enabled=False)
        mock_storage_fn.return_value.list_skills.return_value = [skill]
        result = runner.invoke(skills, ["list"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.skills.formatting.format_skills_json", return_value='[{"name":"test"}]')
    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_json(
        self, mock_storage_fn: MagicMock, _fmt: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.list_skills.return_value = [_mock_skill()]
        result = runner.invoke(
            skills, ["list", "--json"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0

    @patch("gobby.cli.skills.get_skill_tags", return_value=["git", "test"])
    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_with_tags_filter(
        self, mock_storage_fn: MagicMock, _tags: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.list_skills.return_value = [_mock_skill()]
        result = runner.invoke(
            skills, ["list", "--tags", "git"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0

    @patch("gobby.cli.skills.get_skill_tags", return_value=["other"])
    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_tags_filter_no_match(
        self, mock_storage_fn: MagicMock, _tags: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.list_skills.return_value = [_mock_skill()]
        result = runner.invoke(
            skills,
            ["list", "--tags", "nonexistent"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "No skills found" in result.output


# ---------------------------------------------------------------------------
# skills show
# ---------------------------------------------------------------------------
class TestSkillsShow:
    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_not_found(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = None
        result = runner.invoke(
            skills, ["show", "missing"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_not_found_json(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = None
        result = runner.invoke(
            skills, ["show", "missing", "--json"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_found_text(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills, ["show", "test-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "test-skill" in result.output
        assert "MIT" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_found_json(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills, ["show", "test-skill", "--json"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "test-skill"


# ---------------------------------------------------------------------------
# skills install / remove
# ---------------------------------------------------------------------------
class TestSkillsInstall:
    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_success(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {
            "success": True,
            "skill_name": "my-skill",
            "source_type": "github",
        }
        result = runner.invoke(
            skills, ["install", "github:owner/repo"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "my-skill" in result.output

    @patch("gobby.cli.skills.call_skills_tool", return_value=None)
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_comm_failure(
        self, _client: MagicMock, _check: MagicMock, _call: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            skills, ["install", "github:owner/repo"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_error_result(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": False, "error": "Not found"}
        result = runner.invoke(
            skills, ["install", "github:owner/repo"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.check_daemon", return_value=False)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_daemon_not_running(
        self, _client: MagicMock, _check: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            skills, ["install", "github:owner/repo"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1


class TestSkillsRemove:
    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_remove_success(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": True, "skill_name": "my-skill"}
        result = runner.invoke(
            skills, ["remove", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0

    @patch("gobby.cli.skills.call_skills_tool", return_value=None)
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_remove_comm_failure(
        self, _client: MagicMock, _check: MagicMock, _call: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            skills, ["remove", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# skills update
# ---------------------------------------------------------------------------
class TestSkillsUpdate:
    @patch("gobby.cli.skills.check_daemon", return_value=False)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_daemon_not_running(
        self, _client: MagicMock, _check: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            skills, ["update", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_no_name_no_all(
        self, _client: MagicMock, _check: MagicMock, _call: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(skills, ["update"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 1

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_single_success(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": True, "updated": True}
        result = runner.invoke(
            skills, ["update", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "Updated" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_single_skipped(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": True, "updated": False, "skip_reason": "up to date"}
        result = runner.invoke(
            skills, ["update", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "Skipped" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_all(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        # First call: list_skills, second call: update_skill
        mock_call.side_effect = [
            {"success": True, "skills": [{"name": "s1"}, {"name": "s2"}]},
            {"success": True, "updated": True},
            {"success": True, "updated": False, "skip_reason": "local"},
        ]
        result = runner.invoke(
            skills, ["update", "--all"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "Updated 1" in result.output

    @patch("gobby.cli.skills.call_skills_tool", return_value=None)
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_single_comm_failure(
        self, _client: MagicMock, _check: MagicMock, _call: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            skills, ["update", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_single_error(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": False, "error": "Something failed"}
        result = runner.invoke(
            skills, ["update", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# skills enable / disable
# ---------------------------------------------------------------------------
class TestSkillsEnableDisable:
    @patch("gobby.cli.skills.get_skill_storage")
    def test_enable(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills, ["enable", "test-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "Enabled" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_enable_not_found(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = None
        result = runner.invoke(
            skills, ["enable", "missing"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.get_skill_storage")
    def test_enable_error(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        mock_storage_fn.return_value.update_skill.side_effect = RuntimeError("db error")
        result = runner.invoke(
            skills, ["enable", "test-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.get_skill_storage")
    def test_disable(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills, ["disable", "test-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "Disabled" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_disable_not_found(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = None
        result = runner.invoke(
            skills, ["disable", "missing"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.get_skill_storage")
    def test_disable_error(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        mock_storage_fn.return_value.update_skill.side_effect = RuntimeError("db error")
        result = runner.invoke(
            skills, ["disable", "test-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# skills validate
# ---------------------------------------------------------------------------
class TestSkillsValidate:
    def test_validate_path_not_found(self, runner: CliRunner) -> None:
        result = runner.invoke(
            skills,
            ["validate", "/nonexistent/path"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    def test_validate_path_not_found_json(self, runner: CliRunner) -> None:
        result = runner.invoke(
            skills,
            ["validate", "/nonexistent/path", "--json"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("gobby.skills.validator.SkillValidator")
    @patch("gobby.skills.loader.SkillLoader")
    def test_validate_valid(
        self,
        mock_loader_cls: MagicMock,
        mock_validator_cls: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Test")
        parsed = MagicMock(name="test-skill")
        mock_loader_cls.return_value.load_skill.return_value = parsed
        validation_result = MagicMock(valid=True, warnings=[], errors=[])
        mock_validator_cls.return_value.validate.return_value = validation_result
        result = runner.invoke(
            skills, ["validate", str(skill_file)], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0

    @patch("gobby.skills.validator.SkillValidator")
    @patch("gobby.skills.loader.SkillLoader")
    def test_validate_invalid(
        self,
        mock_loader_cls: MagicMock,
        mock_validator_cls: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Test")
        parsed = MagicMock(name="bad-skill")
        mock_loader_cls.return_value.load_skill.return_value = parsed
        validation_result = MagicMock(
            valid=False, errors=["Name too long"], warnings=["No version"]
        )
        mock_validator_cls.return_value.validate.return_value = validation_result
        result = runner.invoke(
            skills, ["validate", str(skill_file)], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1
        assert "Name too long" in result.output

    @patch("gobby.skills.validator.SkillValidator")
    @patch("gobby.skills.loader.SkillLoader")
    def test_validate_valid_json(
        self,
        mock_loader_cls: MagicMock,
        mock_validator_cls: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Test")
        parsed = MagicMock()
        parsed.configure_mock(name="test-skill")
        mock_loader_cls.return_value.load_skill.return_value = parsed
        validation_result = MagicMock(valid=True, warnings=[], errors=[])
        validation_result.to_dict.return_value = {"valid": True, "errors": [], "warnings": []}
        mock_validator_cls.return_value.validate.return_value = validation_result
        result = runner.invoke(
            skills,
            ["validate", str(skill_file), "--json"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    @patch("gobby.skills.loader.SkillLoader")
    def test_validate_load_error(
        self, mock_loader_cls: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        from gobby.skills.loader import SkillLoadError

        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# bad")
        mock_loader_cls.return_value.load_skill.side_effect = SkillLoadError("parse error")
        result = runner.invoke(
            skills, ["validate", str(skill_file)], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# skills meta get / set / unset
# ---------------------------------------------------------------------------
class TestSkillsMeta:
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_not_found(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = None
        result = runner.invoke(
            skills,
            ["meta", "get", "missing", "key"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.get_nested_value", return_value="value1")
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_string(
        self, mock_storage_fn: MagicMock, _nested: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills,
            ["meta", "get", "test-skill", "author"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "value1" in result.output

    @patch("gobby.cli.skills.get_nested_value", return_value={"nested": "dict"})
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_dict(
        self, mock_storage_fn: MagicMock, _nested: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills,
            ["meta", "get", "test-skill", "complex"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    @patch("gobby.cli.skills.get_nested_value", return_value=None)
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_key_not_found(
        self, mock_storage_fn: MagicMock, _nested: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills,
            ["meta", "get", "test-skill", "missing"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_no_metadata(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        skill = _mock_skill(metadata=None)
        mock_storage_fn.return_value.get_by_name.return_value = skill
        result = runner.invoke(
            skills,
            ["meta", "get", "test-skill", "author"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "null" in result.output

    @patch("gobby.cli.skills.set_nested_value", return_value={"author": "new"})
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_set(self, mock_storage_fn: MagicMock, _set: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills,
            ["meta", "set", "test-skill", "author", "new"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "Set author" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_set_not_found(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = None
        result = runner.invoke(
            skills,
            ["meta", "set", "missing", "key", "val"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.set_nested_value", return_value={"k": "v"})
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_set_error(
        self, mock_storage_fn: MagicMock, _set: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        mock_storage_fn.return_value.update_skill.side_effect = RuntimeError("db err")
        result = runner.invoke(
            skills,
            ["meta", "set", "test-skill", "k", "v"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.unset_nested_value", return_value={})
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_unset(
        self, mock_storage_fn: MagicMock, _unset: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        result = runner.invoke(
            skills,
            ["meta", "unset", "test-skill", "author"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "Unset author" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_unset_not_found(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = None
        result = runner.invoke(
            skills,
            ["meta", "unset", "missing", "key"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_unset_no_metadata(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        skill = _mock_skill(metadata=None)
        mock_storage_fn.return_value.get_by_name.return_value = skill
        result = runner.invoke(
            skills,
            ["meta", "unset", "test-skill", "key"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    @patch("gobby.cli.skills.unset_nested_value", return_value={})
    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_unset_error(
        self, mock_storage_fn: MagicMock, _unset: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.get_by_name.return_value = _mock_skill()
        mock_storage_fn.return_value.update_skill.side_effect = RuntimeError("db err")
        result = runner.invoke(
            skills,
            ["meta", "unset", "test-skill", "key"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# skills init / new
# ---------------------------------------------------------------------------
class TestSkillsInit:
    @patch("gobby.skills.scaffold.init_skills_directory")
    def test_init_created(self, mock_init: MagicMock, runner: CliRunner) -> None:
        mock_init.return_value = {"dir_created": True, "config_created": True}
        result = runner.invoke(skills, ["init"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 0
        assert "initialized" in result.output.lower()

    @patch("gobby.skills.scaffold.init_skills_directory")
    def test_init_already_exists(self, mock_init: MagicMock, runner: CliRunner) -> None:
        mock_init.return_value = {"dir_created": False, "config_created": False}
        result = runner.invoke(skills, ["init"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 0
        assert "already exists" in result.output


class TestSkillsNew:
    @patch("gobby.skills.scaffold.scaffold_skill")
    def test_new_success(self, mock_scaffold: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(
            skills, ["new", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "my-skill" in result.output

    @patch("gobby.skills.scaffold.scaffold_skill", side_effect=ValueError("bad name"))
    def test_new_value_error(self, _scaffold: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(
            skills, ["new", "BAD NAME"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1

    @patch("gobby.skills.scaffold.scaffold_skill", side_effect=FileExistsError("already there"))
    def test_new_exists(self, _scaffold: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(
            skills, ["new", "my-skill"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# skills doc
# ---------------------------------------------------------------------------
class TestSkillsDoc:
    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_no_skills(self, mock_storage_fn: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.list_skills.return_value = []
        result = runner.invoke(skills, ["doc"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 0
        assert "No skills" in result.output

    @patch("gobby.skills.formatting.format_skills_markdown_table", return_value="| Name | Desc |")
    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_markdown(
        self, mock_storage_fn: MagicMock, _fmt: MagicMock, runner: CliRunner
    ) -> None:
        mock_storage_fn.return_value.list_skills.return_value = [_mock_skill()]
        result = runner.invoke(skills, ["doc"], obj=_make_config_obj(), catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.skills.formatting.format_skills_json", return_value='[{"name": "s"}]')
    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_json(self, mock_storage_fn: MagicMock, _fmt: MagicMock, runner: CliRunner) -> None:
        mock_storage_fn.return_value.list_skills.return_value = [_mock_skill()]
        result = runner.invoke(
            skills, ["doc", "--format", "json"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0

    @patch("gobby.skills.formatting.format_skills_markdown_table", return_value="content")
    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_output_file(
        self, mock_storage_fn: MagicMock, _fmt: MagicMock, runner: CliRunner, tmp_path: Path
    ) -> None:
        mock_storage_fn.return_value.list_skills.return_value = [_mock_skill()]
        out = tmp_path / "skills.md"
        result = runner.invoke(
            skills, ["doc", "--output", str(out)], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert out.exists()


# ---------------------------------------------------------------------------
# skills search
# ---------------------------------------------------------------------------
class TestSkillsSearch:
    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_search_results(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {
            "success": True,
            "results": [
                {
                    "hub_name": "clawdhub",
                    "slug": "commit-message",
                    "display_name": "Commit Message",
                    "description": "Generate commit messages",
                },
            ],
        }
        result = runner.invoke(
            skills, ["search", "commit"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "Commit Message" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_search_no_results(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": True, "results": []}
        result = runner.invoke(
            skills, ["search", "zzzzz"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "No skills found" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_search_json(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": True, "results": [{"slug": "s1"}]}
        result = runner.invoke(
            skills, ["search", "test", "--json"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0

    @patch("gobby.cli.skills.call_skills_tool", return_value=None)
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_search_comm_failure(
        self, _client: MagicMock, _check: MagicMock, _call: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            skills, ["search", "test"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# skills hub list / hub add
# ---------------------------------------------------------------------------
class TestSkillsHub:
    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_hub_list(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {
            "success": True,
            "hubs": [{"name": "clawdhub", "type": "clawdhub", "base_url": ""}],
        }
        result = runner.invoke(
            skills, ["hub", "list"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "clawdhub" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_hub_list_empty(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": True, "hubs": []}
        result = runner.invoke(
            skills, ["hub", "list"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0
        assert "No hubs" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon", return_value=True)
    @patch("gobby.cli.skills.get_daemon_client")
    def test_hub_list_json(
        self, _client: MagicMock, _check: MagicMock, mock_call: MagicMock, runner: CliRunner
    ) -> None:
        mock_call.return_value = {"success": True, "hubs": [{"name": "h1"}]}
        result = runner.invoke(
            skills, ["hub", "list", "--json"], obj=_make_config_obj(), catch_exceptions=False
        )
        assert result.exit_code == 0

    def test_hub_add_invalid_type(self, runner: CliRunner) -> None:
        result = runner.invoke(
            skills,
            ["hub", "add", "test-hub", "--type", "invalid"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    def test_hub_add_skillsmp_no_url(self, runner: CliRunner) -> None:
        result = runner.invoke(
            skills,
            ["hub", "add", "test-hub", "--type", "skillsmp"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    def test_hub_add_github_no_repo(self, runner: CliRunner) -> None:
        result = runner.invoke(
            skills,
            ["hub", "add", "test-hub", "--type", "github"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1

    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.cli.utils.load_full_config_from_db")
    def test_hub_add_success(
        self,
        mock_config: MagicMock,
        mock_store_cls: MagicMock,
        mock_db_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_config.return_value.database_path = "/tmp/test.db"
        mock_store_cls.return_value.get.return_value = None
        result = runner.invoke(
            skills,
            ["hub", "add", "my-hub", "--type", "skillsmp", "--url", "https://hub.example.com"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "Added hub" in result.output

    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.cli.utils.load_full_config_from_db")
    def test_hub_add_already_exists(
        self,
        mock_config: MagicMock,
        mock_store_cls: MagicMock,
        mock_db_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_config.return_value.database_path = "/tmp/test.db"
        mock_store_cls.return_value.get.return_value = "skillsmp"
        result = runner.invoke(
            skills,
            ["hub", "add", "my-hub", "--type", "skillsmp", "--url", "https://hub.example.com"],
            obj=_make_config_obj(),
            catch_exceptions=False,
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
class TestHelperFunctions:
    def test_get_daemon_client_no_config(self, runner: CliRunner) -> None:
        from gobby.cli.skills import get_daemon_client

        ctx = MagicMock(spec=["obj"])
        ctx.obj = None
        with pytest.raises(click.ClickException):
            get_daemon_client(ctx)

    def test_get_daemon_client_wrong_type(self, runner: CliRunner) -> None:
        from gobby.cli.skills import get_daemon_client

        ctx = MagicMock(spec=["obj"])
        ctx.obj = {"config": "not-a-DaemonConfig"}
        with pytest.raises(click.ClickException):
            get_daemon_client(ctx)

    @patch("gobby.cli.skills.DaemonClient")
    def test_check_daemon_not_healthy(self, mock_client_cls: MagicMock) -> None:
        from gobby.cli.skills import check_daemon

        client = MagicMock()
        client.check_health.return_value = (False, "Connection refused")
        assert check_daemon(client) is False

    @patch("gobby.cli.skills.DaemonClient")
    def test_call_skills_tool_success(self, mock_client_cls: MagicMock) -> None:
        from gobby.cli.skills import call_skills_tool

        client = MagicMock()
        client.call_mcp_tool.return_value = {"success": True, "result": {"key": "val"}}
        result = call_skills_tool(client, "test_tool", {"arg": 1})
        assert result == {"key": "val"}

    @patch("gobby.cli.skills.DaemonClient")
    def test_call_skills_tool_failure(self, mock_client_cls: MagicMock) -> None:
        from gobby.cli.skills import call_skills_tool

        client = MagicMock()
        client.call_mcp_tool.return_value = {"success": False}
        result = call_skills_tool(client, "test_tool", {})
        assert result is None

    @patch("gobby.cli.skills.DaemonClient")
    def test_call_skills_tool_exception(self, mock_client_cls: MagicMock) -> None:
        from gobby.cli.skills import call_skills_tool

        client = MagicMock()
        client.call_mcp_tool.side_effect = ConnectionError("refused")
        result = call_skills_tool(client, "test_tool", {})
        assert result is None
