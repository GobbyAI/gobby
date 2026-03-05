"""Tests for the skills CLI module (TDD - written before implementation)."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.cli


class TestSkillsCommandGroup:
    """Tests for gobby skills command group."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_skills_group_exists(self, runner: CliRunner) -> None:
        """Test that skills command group is registered."""
        result = runner.invoke(cli, ["skills", "--help"])
        assert result.exit_code == 0
        assert "skills" in result.output.lower() or "skill" in result.output.lower()

    def test_skills_help_shows_commands(self, runner: CliRunner) -> None:
        """Test that --help shows available subcommands."""
        result = runner.invoke(cli, ["skills", "--help"])
        assert result.exit_code == 0
        # Should show at least these commands
        assert "list" in result.output
        assert "show" in result.output


class TestSkillsListCommand:
    """Tests for gobby skills list command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_list_help(self, runner: CliRunner) -> None:
        """Test skills list --help."""
        result = runner.invoke(cli, ["skills", "list", "--help"])
        assert result.exit_code == 0
        assert "List" in result.output or "list" in result.output

    def test_list_help_shows_json_flag(self, runner: CliRunner) -> None:
        """Test skills list --help shows --json flag."""
        result = runner.invoke(cli, ["skills", "list", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output

    def test_list_help_shows_tags_flag(self, runner: CliRunner) -> None:
        """Test skills list --help shows --tags flag."""
        result = runner.invoke(cli, ["skills", "list", "--help"])
        assert result.exit_code == 0
        assert "--tags" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_no_skills(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test listing skills when none exist."""
        mock_storage = MagicMock()
        mock_storage.list_skills.return_value = []
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "list"])

        assert result.exit_code == 0
        assert "No skills found" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_with_skills(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test listing skills with results."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill"
        mock_skill.enabled = True
        mock_skill.metadata = None
        mock_storage.list_skills.return_value = [mock_skill]
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "list"])

        assert result.exit_code == 0
        assert "test-skill" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_json_output(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test listing skills with JSON output."""
        import json

        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill"
        mock_skill.enabled = True
        mock_skill.version = "1.0.0"
        mock_skill.metadata = {"skillport": {"category": "test", "tags": ["tag1"]}}
        mock_storage.list_skills.return_value = [mock_skill]
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "list", "--json"])

        assert result.exit_code == 0
        # Parse JSON output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "test-skill"
        assert data[0]["category"] == "test"
        assert data[0]["tags"] == ["tag1"]

    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_empty_json_output(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test listing no skills with JSON output."""
        import json

        mock_storage = MagicMock()
        mock_storage.list_skills.return_value = []
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_with_tags_filter(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test listing skills with tags filter."""
        mock_storage = MagicMock()
        mock_skill1 = MagicMock()
        mock_skill1.name = "skill-with-tag"
        mock_skill1.description = "Has matching tag"
        mock_skill1.enabled = True
        mock_skill1.metadata = {"skillport": {"tags": ["git", "workflow"]}}

        mock_skill2 = MagicMock()
        mock_skill2.name = "skill-no-tag"
        mock_skill2.description = "No matching tag"
        mock_skill2.enabled = True
        mock_skill2.metadata = {"skillport": {"tags": ["other"]}}

        mock_storage.list_skills.return_value = [mock_skill1, mock_skill2]
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "list", "--tags", "git"])

        assert result.exit_code == 0
        assert "skill-with-tag" in result.output
        assert "skill-no-tag" not in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_list_with_category_display(
        self, mock_get_storage: MagicMock, runner: CliRunner
    ) -> None:
        """Test that category is displayed in list output."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill"
        mock_skill.enabled = True
        mock_skill.metadata = {"skillport": {"category": "git"}}
        mock_storage.list_skills.return_value = [mock_skill]
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "list"])

        assert result.exit_code == 0
        assert "[git]" in result.output


class TestSkillsShowCommand:
    """Tests for gobby skills show command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_show_help(self, runner: CliRunner) -> None:
        """Test skills show --help."""
        result = runner.invoke(cli, ["skills", "show", "--help"])
        assert result.exit_code == 0
        assert "Show" in result.output or "show" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_not_found(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test showing non-existent skill."""
        mock_storage = MagicMock()
        mock_storage.get_by_name.return_value = None
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "show", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_success(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test showing an existing skill."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill for demonstration"
        mock_skill.version = "1.0.0"
        mock_skill.license = "MIT"
        mock_skill.enabled = True
        mock_skill.source_path = "/path/to/skill"
        mock_skill.source_type = "local"
        mock_skill.content = "# Test Skill\n\nInstructions here."
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "show", "test-skill"])

        assert result.exit_code == 0
        assert "test-skill" in result.output
        assert "A test skill for demonstration" in result.output

    def test_show_help_shows_json_flag(self, runner: CliRunner) -> None:
        """Test skills show --help shows --json flag."""
        result = runner.invoke(cli, ["skills", "show", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_json_output(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test showing skill with JSON output."""
        import json

        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill for demonstration"
        mock_skill.version = "1.0.0"
        mock_skill.license = "MIT"
        mock_skill.enabled = True
        mock_skill.source_path = "/path/to/skill"
        mock_skill.source_type = "local"
        mock_skill.compatibility = "Requires Python 3.11+"
        mock_skill.content = "# Test Skill\n\nInstructions here."
        mock_skill.metadata = {"skillport": {"category": "test", "tags": ["demo"]}}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "show", "test-skill", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "test-skill"
        assert data["description"] == "A test skill for demonstration"
        assert data["version"] == "1.0.0"
        assert data["license"] == "MIT"
        assert data["enabled"] is True
        assert data["content"] == "# Test Skill\n\nInstructions here."
        assert data["category"] == "test"
        assert data["tags"] == ["demo"]

    @patch("gobby.cli.skills.get_skill_storage")
    def test_show_json_not_found(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test showing non-existent skill with JSON output."""
        import json

        mock_storage = MagicMock()
        mock_storage.get_by_name.return_value = None
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "show", "nonexistent", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"] == "Skill not found"
        assert data["name"] == "nonexistent"


class TestSkillsInstallCommand:
    """Tests for gobby skills install command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_install_help(self, runner: CliRunner) -> None:
        """Test skills install --help."""
        result = runner.invoke(cli, ["skills", "install", "--help"])
        assert result.exit_code == 0
        assert "Install" in result.output or "install" in result.output

    def test_install_help_shows_project_flag(self, runner: CliRunner) -> None:
        """Test skills install --help shows --project flag."""
        result = runner.invoke(cli, ["skills", "install", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output

    def test_install_requires_source(self, runner: CliRunner) -> None:
        """Test that install requires source argument."""
        result = runner.invoke(cli, ["skills", "install"])
        # Should show missing argument error
        assert result.exit_code != 0

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_from_local_path(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test installing from a local directory path."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "skill_name": "test-skill",
            "source_type": "local",
        }

        with runner.isolated_filesystem():
            import os

            os.makedirs("my-skill")
            with open("my-skill/SKILL.md", "w") as f:
                f.write("---\nname: test-skill\ndescription: A test skill\n---\n# Test")

            result = runner.invoke(cli, ["skills", "install", "my-skill"])

        assert result.exit_code == 0
        assert "Installed skill: test-skill" in result.output
        mock_call_tool.assert_called_once()

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_from_github(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test installing from a GitHub URL."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "skill_name": "github-skill",
            "source_type": "github",
        }

        result = runner.invoke(cli, ["skills", "install", "github:owner/repo"])

        assert result.exit_code == 0
        assert "Installed skill: github-skill" in result.output
        assert "github" in result.output.lower()
        mock_call_tool.assert_called_once()
        call_args = mock_call_tool.call_args
        assert call_args[0][1] == "install_skill"
        assert call_args[0][2]["source"] == "github:owner/repo"

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_with_project_flag(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test installing with --project flag scopes to project."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "skill_name": "project-skill",
            "source_type": "github",
        }

        result = runner.invoke(cli, ["skills", "install", "github:owner/repo", "--project"])

        assert result.exit_code == 0
        # Verify call_skills_tool was called with project_scoped=True
        call_args = mock_call_tool.call_args
        assert call_args[0][2]["project_scoped"] is True

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_source_not_found(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test installing from non-existent source."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": False,
            "error": "Source not found: /nonexistent/path/to/skill",
        }

        result = runner.invoke(cli, ["skills", "install", "/nonexistent/path/to/skill"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_install_from_hub(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test installing from a hub using hub:slug syntax."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "skill_name": "commit-message",
            "source_type": "hub",
        }

        result = runner.invoke(cli, ["skills", "install", "clawdhub:commit-message"])

        assert result.exit_code == 0
        assert "Installed skill: commit-message" in result.output
        assert "hub" in result.output.lower()
        mock_call_tool.assert_called_once()
        call_args = mock_call_tool.call_args
        assert call_args[0][1] == "install_skill"
        assert call_args[0][2]["source"] == "clawdhub:commit-message"

    def test_install_help_shows_hub_syntax(self, runner: CliRunner) -> None:
        """Test that install help shows hub:slug syntax."""
        result = runner.invoke(cli, ["skills", "install", "--help"])
        assert result.exit_code == 0
        # Should mention hub:slug format in help
        assert "hub" in result.output.lower()


class TestSkillsEnableDisableCommands:
    """Tests for gobby skills enable/disable commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_enable_help(self, runner: CliRunner) -> None:
        """Test skills enable --help."""
        result = runner.invoke(cli, ["skills", "enable", "--help"])
        assert result.exit_code == 0
        assert "enable" in result.output.lower()

    def test_disable_help(self, runner: CliRunner) -> None:
        """Test skills disable --help."""
        result = runner.invoke(cli, ["skills", "disable", "--help"])
        assert result.exit_code == 0
        assert "disable" in result.output.lower()

    def test_enable_requires_name(self, runner: CliRunner) -> None:
        """Test that enable requires name argument."""
        result = runner.invoke(cli, ["skills", "enable"])
        assert result.exit_code != 0

    def test_disable_requires_name(self, runner: CliRunner) -> None:
        """Test that disable requires name argument."""
        result = runner.invoke(cli, ["skills", "disable"])
        assert result.exit_code != 0

    @patch("gobby.cli.skills.get_skill_storage")
    def test_enable_skill(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test enabling a skill."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.name = "test-skill"
        mock_skill.enabled = False
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "enable", "test-skill"])

        assert result.exit_code == 0
        assert "enabled" in result.output.lower()
        mock_storage.update_skill.assert_called_once_with("skl-123", enabled=True)

    @patch("gobby.cli.skills.get_skill_storage")
    def test_disable_skill(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test disabling a skill."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.name = "test-skill"
        mock_skill.enabled = True
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "disable", "test-skill"])

        assert result.exit_code == 0
        assert "disabled" in result.output.lower()
        mock_storage.update_skill.assert_called_once_with("skl-123", enabled=False)

    @patch("gobby.cli.skills.get_skill_storage")
    def test_enable_skill_not_found(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test enabling a non-existent skill."""
        mock_storage = MagicMock()
        mock_storage.get_by_name.return_value = None
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "enable", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_disable_skill_not_found(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test disabling a non-existent skill."""
        mock_storage = MagicMock()
        mock_storage.get_by_name.return_value = None
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "disable", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestSkillsDocCommand:
    """Tests for gobby skills doc command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_doc_help(self, runner: CliRunner) -> None:
        """Test skills doc --help."""
        result = runner.invoke(cli, ["skills", "doc", "--help"])
        assert result.exit_code == 0
        assert "doc" in result.output.lower()

    def test_doc_help_shows_output_flag(self, runner: CliRunner) -> None:
        """Test skills doc --help shows --output flag."""
        result = runner.invoke(cli, ["skills", "doc", "--help"])
        assert result.exit_code == 0
        assert "--output" in result.output

    def test_doc_help_shows_format_flag(self, runner: CliRunner) -> None:
        """Test skills doc --help shows --format flag."""
        result = runner.invoke(cli, ["skills", "doc", "--help"])
        assert result.exit_code == 0
        assert "--format" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_outputs_markdown_table(
        self, mock_get_storage: MagicMock, runner: CliRunner
    ) -> None:
        """Test that doc outputs markdown table."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill"
        mock_skill.enabled = True
        mock_skill.metadata = {"skillport": {"category": "test"}}
        mock_storage.list_skills.return_value = [mock_skill]
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "doc"])

        assert result.exit_code == 0
        # Should have markdown table
        assert "|" in result.output
        assert "test-skill" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_no_skills(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test doc when no skills installed."""
        mock_storage = MagicMock()
        mock_storage.list_skills.return_value = []
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "doc"])

        assert result.exit_code == 0
        assert "No skills" in result.output or "no skills" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_format_json(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test doc with --format json."""
        import json

        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill"
        mock_skill.enabled = True
        mock_skill.version = "1.0.0"
        mock_skill.metadata = {"skillport": {"category": "test", "tags": ["demo"]}}
        mock_storage.list_skills.return_value = [mock_skill]
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "doc", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "test-skill"

    @patch("gobby.cli.skills.get_skill_storage")
    def test_doc_output_to_file(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test doc with --output writes to file."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.name = "test-skill"
        mock_skill.description = "A test skill"
        mock_skill.enabled = True
        mock_skill.metadata = {"skillport": {"category": "test"}}
        mock_storage.list_skills.return_value = [mock_skill]
        mock_get_storage.return_value = mock_storage

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "doc", "--output", "SKILLS.md"])

            assert result.exit_code == 0
            import os

            assert os.path.isfile("SKILLS.md")
            with open("SKILLS.md") as f:
                content = f.read()
            assert "test-skill" in content


class TestSkillsNewCommand:
    """Tests for gobby skills new command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_new_help(self, runner: CliRunner) -> None:
        """Test skills new --help."""
        result = runner.invoke(cli, ["skills", "new", "--help"])
        assert result.exit_code == 0
        assert "new" in result.output.lower()

    def test_new_requires_name(self, runner: CliRunner) -> None:
        """Test that new requires name argument."""
        result = runner.invoke(cli, ["skills", "new"])
        # Should show missing argument error
        assert result.exit_code != 0

    def test_new_creates_skill_directory(self, runner: CliRunner) -> None:
        """Test that new creates skill directory."""
        import os

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "new", "my-skill"])

            assert result.exit_code == 0
            assert os.path.isdir("my-skill")

    def test_new_creates_skill_md(self, runner: CliRunner) -> None:
        """Test that new creates SKILL.md file."""
        import os

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "new", "my-skill"])

            assert result.exit_code == 0
            assert os.path.isfile("my-skill/SKILL.md")

    def test_new_creates_scripts_directory(self, runner: CliRunner) -> None:
        """Test that new creates scripts/ directory."""
        import os

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "new", "my-skill"])

            assert result.exit_code == 0
            assert os.path.isdir("my-skill/scripts")

    def test_new_creates_assets_directory(self, runner: CliRunner) -> None:
        """Test that new creates assets/ directory."""
        import os

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "new", "my-skill"])

            assert result.exit_code == 0
            assert os.path.isdir("my-skill/assets")

    def test_new_creates_references_directory(self, runner: CliRunner) -> None:
        """Test that new creates references/ directory."""
        import os

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "new", "my-skill"])

            assert result.exit_code == 0
            assert os.path.isdir("my-skill/references")

    def test_new_skill_md_has_frontmatter(self, runner: CliRunner) -> None:
        """Test that SKILL.md has valid frontmatter."""
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "new", "my-skill"])

            assert result.exit_code == 0
            with open("my-skill/SKILL.md") as f:
                content = f.read()
            assert content.startswith("---")
            assert "name: my-skill" in content

    def test_new_skill_already_exists(self, runner: CliRunner) -> None:
        """Test new when skill directory already exists."""
        import os

        with runner.isolated_filesystem():
            os.makedirs("my-skill")
            result = runner.invoke(cli, ["skills", "new", "my-skill"])

            assert result.exit_code == 1
            assert "exists" in result.output.lower()

    def test_new_with_description(self, runner: CliRunner) -> None:
        """Test new with --description flag."""
        with runner.isolated_filesystem():
            result = runner.invoke(
                cli, ["skills", "new", "my-skill", "--description", "A custom description"]
            )

            assert result.exit_code == 0
            with open("my-skill/SKILL.md") as f:
                content = f.read()
            assert "A custom description" in content


class TestSkillsInitCommand:
    """Tests for gobby skills init command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_init_help(self, runner: CliRunner) -> None:
        """Test skills init --help."""
        result = runner.invoke(cli, ["skills", "init", "--help"])
        assert result.exit_code == 0
        assert "init" in result.output.lower()

    def test_init_creates_skills_directory(self, runner: CliRunner) -> None:
        """Test that init creates .gobby/skills/ directory."""
        import os

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "init"])

            assert result.exit_code == 0
            assert os.path.isdir(".gobby/skills")

    def test_init_creates_config_file(self, runner: CliRunner) -> None:
        """Test that init creates skills config file."""
        import os

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "init"])

            assert result.exit_code == 0
            # Should create a config file
            assert os.path.isfile(".gobby/skills/config.yaml") or os.path.isfile(
                ".gobby/skills/config.json"
            )

    def test_init_idempotent(self, runner: CliRunner) -> None:
        """Test that init can be run multiple times safely."""
        import os

        with runner.isolated_filesystem():
            # Run init twice
            result1 = runner.invoke(cli, ["skills", "init"])
            result2 = runner.invoke(cli, ["skills", "init"])

            assert result1.exit_code == 0
            assert result2.exit_code == 0
            assert os.path.isdir(".gobby/skills")

    def test_init_with_existing_gobby_dir(self, runner: CliRunner) -> None:
        """Test init when .gobby directory already exists."""
        import os

        with runner.isolated_filesystem():
            os.makedirs(".gobby/tasks")
            result = runner.invoke(cli, ["skills", "init"])

            assert result.exit_code == 0
            assert os.path.isdir(".gobby/skills")
            assert os.path.isdir(".gobby/tasks")  # Should preserve existing dirs

    def test_init_output_message(self, runner: CliRunner) -> None:
        """Test that init shows success message."""
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["skills", "init"])

            assert result.exit_code == 0
            assert (
                "initialized" in result.output.lower()
                or "created" in result.output.lower()
                or "skills" in result.output.lower()
            )


class TestSkillsMetaCommand:
    """Tests for gobby skills meta command group."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_meta_help(self, runner: CliRunner) -> None:
        """Test skills meta --help."""
        result = runner.invoke(cli, ["skills", "meta", "--help"])
        assert result.exit_code == 0
        assert "meta" in result.output.lower()

    def test_meta_help_shows_subcommands(self, runner: CliRunner) -> None:
        """Test skills meta --help shows get/set/unset subcommands."""
        result = runner.invoke(cli, ["skills", "meta", "--help"])
        assert result.exit_code == 0
        assert "get" in result.output
        assert "set" in result.output
        assert "unset" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_simple_key(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test getting a simple metadata key."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.metadata = {"author": "test-author", "version": "1.0.0"}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "meta", "get", "test-skill", "author"])

        assert result.exit_code == 0
        assert "test-author" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_nested_key(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test getting a nested metadata key with dot notation."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.metadata = {"skillport": {"category": "git", "tags": ["test"]}}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "meta", "get", "test-skill", "skillport.category"])

        assert result.exit_code == 0
        assert "git" in result.output

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_get_key_not_found(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test getting a non-existent metadata key."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.metadata = {"author": "test"}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "meta", "get", "test-skill", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "null" in result.output.lower()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_set_simple_key(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test setting a simple metadata key."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.metadata = {"author": "old-author"}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "meta", "set", "test-skill", "author", "new-author"])

        assert result.exit_code == 0
        mock_storage.update_skill.assert_called_once()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_set_nested_key(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test setting a nested metadata key with dot notation."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.metadata = {"skillport": {"category": "old"}}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(
            cli, ["skills", "meta", "set", "test-skill", "skillport.category", "new"]
        )

        assert result.exit_code == 0
        mock_storage.update_skill.assert_called_once()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_unset_simple_key(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test unsetting a simple metadata key."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.metadata = {"author": "test", "version": "1.0.0"}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "meta", "unset", "test-skill", "author"])

        assert result.exit_code == 0
        mock_storage.update_skill.assert_called_once()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_unset_nested_key(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test unsetting a nested metadata key with dot notation."""
        mock_storage = MagicMock()
        mock_skill = MagicMock()
        mock_skill.id = "skl-123"
        mock_skill.metadata = {"skillport": {"category": "git", "tags": ["test"]}}
        mock_storage.get_by_name.return_value = mock_skill
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "meta", "unset", "test-skill", "skillport.tags"])

        assert result.exit_code == 0
        mock_storage.update_skill.assert_called_once()

    @patch("gobby.cli.skills.get_skill_storage")
    def test_meta_skill_not_found(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        """Test meta command when skill not found."""
        mock_storage = MagicMock()
        mock_storage.get_by_name.return_value = None
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["skills", "meta", "get", "nonexistent", "author"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestSkillsValidateCommand:
    """Tests for gobby skills validate command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_validate_help(self, runner: CliRunner) -> None:
        """Test skills validate --help."""
        result = runner.invoke(cli, ["skills", "validate", "--help"])
        assert result.exit_code == 0
        assert "validate" in result.output.lower()

    def test_validate_help_shows_json_flag(self, runner: CliRunner) -> None:
        """Test skills validate --help shows --json flag."""
        result = runner.invoke(cli, ["skills", "validate", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output

    def test_validate_requires_path(self, runner: CliRunner) -> None:
        """Test that validate requires path argument."""
        result = runner.invoke(cli, ["skills", "validate"])
        # Should show missing argument error
        assert result.exit_code != 0

    def test_validate_valid_skill(self, runner: CliRunner) -> None:
        """Test validating a valid skill file."""
        with runner.isolated_filesystem():
            import os

            os.makedirs("test-skill")
            with open("test-skill/SKILL.md", "w") as f:
                f.write(
                    """---
name: test-skill
description: A valid test skill
version: 1.0.0
metadata:
  skillport:
    category: testing
    tags:
      - test
      - demo
---

# Test Skill

Instructions here.
"""
                )

            result = runner.invoke(cli, ["skills", "validate", "test-skill"])

            assert result.exit_code == 0
            assert "valid" in result.output.lower()

    def test_validate_invalid_skill(self, runner: CliRunner) -> None:
        """Test validating an invalid skill file."""
        with runner.isolated_filesystem():
            import os

            os.makedirs("bad-skill")
            with open("bad-skill/SKILL.md", "w") as f:
                f.write(
                    """---
name: Bad_Skill_Name
description: ""
---

# Bad Skill
"""
                )

            result = runner.invoke(cli, ["skills", "validate", "bad-skill"])

            assert result.exit_code == 1
            # Should show errors for invalid name and empty description
            assert "error" in result.output.lower() or "invalid" in result.output.lower()

    def test_validate_json_output_valid(self, runner: CliRunner) -> None:
        """Test validating with JSON output for valid skill."""
        import json

        with runner.isolated_filesystem():
            import os

            os.makedirs("test-skill")
            with open("test-skill/SKILL.md", "w") as f:
                f.write(
                    """---
name: test-skill
description: A valid test skill
---

# Test Skill
"""
                )

            result = runner.invoke(cli, ["skills", "validate", "test-skill", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["valid"] is True
            assert data["errors"] == []

    def test_validate_json_output_invalid(self, runner: CliRunner) -> None:
        """Test validating with JSON output for invalid skill."""
        import json

        with runner.isolated_filesystem():
            import os

            os.makedirs("bad-skill")
            with open("bad-skill/SKILL.md", "w") as f:
                f.write(
                    """---
name: BAD_NAME
description: A skill
---

# Bad
"""
                )

            result = runner.invoke(cli, ["skills", "validate", "bad-skill", "--json"])

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["valid"] is False
            assert len(data["errors"]) > 0

    def test_validate_path_not_found(self, runner: CliRunner) -> None:
        """Test validating non-existent path."""
        result = runner.invoke(cli, ["skills", "validate", "/nonexistent/path"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()


class TestSkillsUpdateCommand:
    """Tests for gobby skills update command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_update_help(self, runner: CliRunner) -> None:
        """Test skills update --help."""
        result = runner.invoke(cli, ["skills", "update", "--help"])
        assert result.exit_code == 0
        assert "Update" in result.output or "update" in result.output

    def test_update_help_shows_all_flag(self, runner: CliRunner) -> None:
        """Test skills update --help shows --all flag."""
        result = runner.invoke(cli, ["skills", "update", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_single_skill(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test updating a single skill by name."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "updated": True,
        }

        result = runner.invoke(cli, ["skills", "update", "test-skill"])

        assert result.exit_code == 0
        assert "Updated" in result.output or "updated" in result.output
        mock_call_tool.assert_called_once()
        call_args = mock_call_tool.call_args
        assert call_args[0][1] == "update_skill"
        assert call_args[0][2]["name"] == "test-skill"

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_skill_not_found(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test updating a non-existent skill."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": False,
            "error": "Skill not found: nonexistent",
        }

        result = runner.invoke(cli, ["skills", "update", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_all_skills(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test updating all skills with --all flag."""
        mock_check_daemon.return_value = True

        # list_skills returns skill list, update_skill returns different results
        def mock_tool_call(client, tool_name, args, timeout=30.0):
            if tool_name == "list_skills":
                return {
                    "success": True,
                    "skills": [
                        {"name": "skill-1"},
                        {"name": "skill-2"},
                    ],
                }
            elif tool_name == "update_skill":
                if args["name"] == "skill-1":
                    return {"success": True, "updated": True}
                else:
                    return {"success": True, "updated": False, "skip_reason": "local source"}
            return {"success": False}

        mock_call_tool.side_effect = mock_tool_call

        result = runner.invoke(cli, ["skills", "update", "--all"])

        assert result.exit_code == 0
        # One GitHub skill updated, one local skipped
        assert "Updated" in result.output or "updated" in result.output
        assert "Skipped" in result.output or "skipped" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_update_local_skill_skipped(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test that local skills without remote source are skipped."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "updated": False,
            "skip_reason": "local skills cannot be updated from remote",
        }

        result = runner.invoke(cli, ["skills", "update", "local-skill"])

        assert result.exit_code == 0
        # Local skills can't be updated from remote
        assert (
            "skip" in result.output.lower()
            or "cannot" in result.output.lower()
            or "local" in result.output.lower()
        )


class TestSkillsRemoveCommand:
    """Tests for gobby skills remove command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_remove_help(self, runner: CliRunner) -> None:
        """Test skills remove --help."""
        result = runner.invoke(cli, ["skills", "remove", "--help"])
        assert result.exit_code == 0
        assert "Remove" in result.output or "remove" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_remove_not_found(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test removing non-existent skill."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": False,
            "error": "Skill not found: nonexistent",
        }

        result = runner.invoke(cli, ["skills", "remove", "nonexistent"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_remove_success(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test removing an existing skill."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "skill_name": "test-skill",
        }

        result = runner.invoke(cli, ["skills", "remove", "test-skill"])

        assert result.exit_code == 0
        assert "Removed" in result.output or "removed" in result.output
        mock_call_tool.assert_called_once()
        call_args = mock_call_tool.call_args
        assert call_args[0][1] == "remove_skill"
        assert call_args[0][2]["name"] == "test-skill"


class TestSkillsSearchCommand:
    """Tests for gobby skills search command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_search_help(self, runner: CliRunner) -> None:
        """Test skills search --help."""
        result = runner.invoke(cli, ["skills", "search", "--help"])
        assert result.exit_code == 0
        assert "Search" in result.output or "search" in result.output

    def test_search_help_shows_hub_option(self, runner: CliRunner) -> None:
        """Test skills search --help shows --hub option."""
        result = runner.invoke(cli, ["skills", "search", "--help"])
        assert result.exit_code == 0
        assert "--hub" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_search_calls_search_hub(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test search command calls search_hub tool."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "results": [
                {
                    "slug": "commit-message",
                    "display_name": "Commit Message Generator",
                    "description": "Generate conventional commits",
                    "hub_name": "clawdhub",
                },
            ],
        }

        result = runner.invoke(cli, ["skills", "search", "commit"])

        assert result.exit_code == 0
        mock_call_tool.assert_called_once()
        call_args = mock_call_tool.call_args
        assert call_args[0][1] == "search_hub"
        assert call_args[0][2]["query"] == "commit"

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_search_with_hub_filter(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test search command passes hub filter."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {"success": True, "results": []}

        result = runner.invoke(cli, ["skills", "search", "commit", "--hub", "clawdhub"])

        assert result.exit_code == 0
        call_args = mock_call_tool.call_args
        assert call_args[0][2]["hub_name"] == "clawdhub"


class TestSkillsHubListCommand:
    """Tests for gobby skills hub list command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_hub_list_help(self, runner: CliRunner) -> None:
        """Test skills hub list --help."""
        result = runner.invoke(cli, ["skills", "hub", "list", "--help"])
        assert result.exit_code == 0
        assert "List" in result.output or "list" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_hub_list_calls_list_hubs(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test hub list command calls list_hubs tool."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "hubs": [
                {"name": "clawdhub", "type": "clawdhub", "base_url": ""},
                {"name": "skillsmp", "type": "skillsmp", "base_url": "https://skillsmp.com/api/v1"},
            ],
        }

        result = runner.invoke(cli, ["skills", "hub", "list"])

        assert result.exit_code == 0
        mock_call_tool.assert_called_once()
        call_args = mock_call_tool.call_args
        assert call_args[0][1] == "list_hubs"

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_hub_list_shows_hubs(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test hub list displays hub names and types."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {
            "success": True,
            "hubs": [
                {"name": "clawdhub", "type": "clawdhub", "base_url": ""},
            ],
        }

        result = runner.invoke(cli, ["skills", "hub", "list"])

        assert result.exit_code == 0
        assert "clawdhub" in result.output

    @patch("gobby.cli.skills.call_skills_tool")
    @patch("gobby.cli.skills.check_daemon")
    @patch("gobby.cli.skills.get_daemon_client")
    def test_hub_list_no_hubs(
        self,
        mock_get_client: MagicMock,
        mock_check_daemon: MagicMock,
        mock_call_tool: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test hub list with no configured hubs."""
        mock_check_daemon.return_value = True
        mock_call_tool.return_value = {"success": True, "hubs": []}

        result = runner.invoke(cli, ["skills", "hub", "list"])

        assert result.exit_code == 0
        assert "No hubs configured" in result.output


class TestSkillsHubAddCommand:
    """Tests for gobby skills hub add command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_hub_add_help(self, runner: CliRunner) -> None:
        """Test skills hub add --help."""
        result = runner.invoke(cli, ["skills", "hub", "add", "--help"])
        assert result.exit_code == 0
        assert "Add" in result.output or "add" in result.output

    def test_hub_add_requires_name(self, runner: CliRunner) -> None:
        """Test that hub add requires name argument."""
        result = runner.invoke(cli, ["skills", "hub", "add"])
        assert result.exit_code != 0

    def test_hub_add_requires_type(self, runner: CliRunner) -> None:
        """Test that hub add requires --type option."""
        result = runner.invoke(cli, ["skills", "hub", "add", "my-hub"])
        assert result.exit_code != 0
        assert "type" in result.output.lower() or "required" in result.output.lower()

    def test_hub_add_help_shows_type_option(self, runner: CliRunner) -> None:
        """Test that help shows --type option."""
        result = runner.invoke(cli, ["skills", "hub", "add", "--help"])
        assert result.exit_code == 0
        assert "--type" in result.output

    def test_hub_add_help_shows_url_option(self, runner: CliRunner) -> None:
        """Test that help shows --url option."""
        result = runner.invoke(cli, ["skills", "hub", "add", "--help"])
        assert result.exit_code == 0
        assert "--url" in result.output

    def test_hub_add_help_shows_repo_option(self, runner: CliRunner) -> None:
        """Test that help shows --repo option."""
        result = runner.invoke(cli, ["skills", "hub", "add", "--help"])
        assert result.exit_code == 0
        assert "--repo" in result.output
