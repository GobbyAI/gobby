"""Tests for ClawdHubProvider."""

from unittest.mock import AsyncMock, patch

import pytest

from gobby.skills.hubs.base import HubSkillInfo
from gobby.skills.hubs.clawdhub import ClawdHubProvider

pytestmark = pytest.mark.unit


class TestClawdHubProvider:
    """Tests for ClawdHubProvider class."""

    def test_provider_type(self) -> None:
        """Test provider_type returns 'clawdhub'."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        assert provider.provider_type == "clawdhub"

    def test_init_with_hub_name(self) -> None:
        """Test initialization with hub_name."""
        provider = ClawdHubProvider(
            hub_name="my-clawdhub",
            base_url="https://clawdhub.com",
        )
        assert provider.hub_name == "my-clawdhub"

    def test_init_with_auth_token(self) -> None:
        """Test initialization with auth_token."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
            auth_token="secret123",
        )
        assert provider.auth_token == "secret123"


class TestClawdHubProviderCLI:
    """Tests for ClawdHubProvider CLI integration."""

    @pytest.mark.asyncio
    async def test_check_cli_available_success(self) -> None:
        """Test CLI availability check when clawhub is installed."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )

        with patch("gobby.skills.hubs.clawdhub.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate.return_value = (b"1.0.0\n", b"")
            mock_exec.return_value = mock_process

            result = await provider._check_cli_available()
            assert result is True
            assert provider._cli_binary == "clawhub"

            # Verify --cli-version flag is used (not --version)
            mock_exec.assert_called_once_with(
                "clawhub",
                "--cli-version",
                stdout=-1,
                stderr=-1,
            )

    @pytest.mark.asyncio
    async def test_check_cli_available_not_installed(self) -> None:
        """Test CLI availability check when clawhub is not installed."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )

        with patch("gobby.skills.hubs.clawdhub.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.side_effect = FileNotFoundError("clawhub not found")

            result = await provider._check_cli_available()
            assert result is False

    @pytest.mark.asyncio
    async def test_run_cli_command_returns_raw_output(self) -> None:
        """Test running a CLI command returns raw string output."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_binary = "clawhub"

        with patch("gobby.skills.hubs.clawdhub.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate.return_value = (b"some output\n", b"")
            mock_exec.return_value = mock_process

            result = await provider._run_cli_command("search", ["test"])
            assert result == "some output"

    @pytest.mark.asyncio
    async def test_run_cli_json_returns_parsed_json(self) -> None:
        """Test _run_cli_json returns parsed JSON from --json commands."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_binary = "clawhub"

        json_output = '{"skills": [{"name": "test-skill"}]}'

        with patch("gobby.skills.hubs.clawdhub.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate.return_value = (json_output.encode(), b"")
            mock_exec.return_value = mock_process

            result = await provider._run_cli_json("explore", [])
            assert result == {"skills": [{"name": "test-skill"}]}

            # Verify --json flag is added
            call_args = mock_exec.call_args[0]
            assert "--json" in call_args


class TestClawdHubProviderSearch:
    """Tests for ClawdHubProvider search functionality."""

    @pytest.mark.asyncio
    async def test_search_parses_text_output(self) -> None:
        """Test search parses text output (search has no --json)."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = True
        provider._cli_binary = "clawhub"

        # Simulate text output from `clawhub search`
        text_output = (
            "commit-message  v1.0.0  Generate conventional commits\n"
            "code-review  v2.1.0  Review code for issues\n"
        )

        with patch.object(provider, "_run_cli_command", return_value=text_output):
            results = await provider.search("commit", limit=10)

            assert len(results) == 2
            assert all(isinstance(r, HubSkillInfo) for r in results)
            assert results[0].slug == "commit-message"
            assert results[0].version == "1.0.0"
            assert results[0].hub_name == "clawdhub"

    @pytest.mark.asyncio
    async def test_search_empty_results(self) -> None:
        """Test search with no results."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = True
        provider._cli_binary = "clawhub"

        with patch.object(provider, "_run_cli_command", return_value=""):
            results = await provider.search("nonexistent")
            assert results == []

    @pytest.mark.asyncio
    async def test_search_raises_when_cli_unavailable(self) -> None:
        """Test search raises RuntimeError when CLI is not installed."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = False

        with pytest.raises(RuntimeError, match="CLI not installed"):
            await provider.search("test")

    @pytest.mark.asyncio
    async def test_search_skips_spinner_lines(self) -> None:
        """Test search ignores spinner/status lines in output."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = True
        provider._cli_binary = "clawhub"

        text_output = "- Searching\ncommit-message  v1.0.0  Generate commits\n"

        with patch.object(provider, "_run_cli_command", return_value=text_output):
            results = await provider.search("commit")
            assert len(results) == 1
            assert results[0].slug == "commit-message"


class TestClawdHubProviderDiscover:
    """Tests for ClawdHubProvider discover functionality."""

    @pytest.mark.asyncio
    async def test_discover_returns_hub_info(self) -> None:
        """Test discover returns hub configuration."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )

        with patch.object(provider, "_check_cli_available", return_value=True):
            result = await provider.discover()
            assert "cli_available" in result
            assert result["cli_available"] is True
            assert result["hub_name"] == "clawdhub"

    @pytest.mark.asyncio
    async def test_discover_includes_cli_binary(self) -> None:
        """Test discover reports which binary was found."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_binary = "clawhub"

        with patch.object(provider, "_check_cli_available", return_value=True):
            result = await provider.discover()
            assert result["cli_binary"] == "clawhub"


class TestClawdHubProviderListSkills:
    """Tests for ClawdHubProvider list_skills uses explore --json."""

    @pytest.mark.asyncio
    async def test_list_skills_uses_explore_json(self) -> None:
        """Test list_skills uses explore --json for remote listing."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = True
        provider._cli_binary = "clawhub"

        explore_results = {
            "skills": [
                {
                    "slug": "skill-1",
                    "name": "Skill One",
                    "description": "First skill",
                },
            ]
        }

        with patch.object(provider, "_run_cli_json", return_value=explore_results) as mock_json:
            results = await provider.list_skills(limit=10)

            assert len(results) == 1
            assert results[0].slug == "skill-1"
            assert results[0].hub_name == "clawdhub"

            # Verify it calls explore (not list)
            mock_json.assert_called_once_with("explore", ["--limit", "10"])

    @pytest.mark.asyncio
    async def test_list_skills_handles_list_response(self) -> None:
        """Test list_skills handles JSON array response."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = True
        provider._cli_binary = "clawhub"

        explore_results = [
            {"slug": "skill-1", "name": "Skill One", "summary": "First skill"},
        ]

        with patch.object(provider, "_run_cli_json", return_value=explore_results):
            results = await provider.list_skills(limit=10)
            assert len(results) == 1
            assert results[0].slug == "skill-1"


class TestClawdHubProviderGetDetails:
    """Tests for ClawdHubProvider get_skill_details uses inspect."""

    @pytest.mark.asyncio
    async def test_get_skill_details_uses_inspect(self) -> None:
        """Test get_skill_details uses inspect --json."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_binary = "clawhub"

        inspect_result = {
            "slug": "commit-message",
            "name": "Commit Message Generator",
            "description": "Generate conventional commits",
            "version": "1.0.0",
            "versions": ["1.0.0", "0.9.0"],
        }

        with patch.object(provider, "_run_cli_json", return_value=inspect_result) as mock_json:
            result = await provider.get_skill_details("commit-message")

            assert result is not None
            assert result.slug == "commit-message"
            assert result.display_name == "Commit Message Generator"
            mock_json.assert_called_once_with("inspect", ["commit-message"])

    @pytest.mark.asyncio
    async def test_get_skill_details_returns_none_on_error(self) -> None:
        """Test get_skill_details returns None on CLI error."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_binary = "clawhub"

        with patch.object(provider, "_run_cli_json", side_effect=RuntimeError("not found")):
            result = await provider.get_skill_details("nonexistent")
            assert result is None


class TestClawdHubProviderDownload:
    """Tests for ClawdHubProvider download functionality."""

    @pytest.mark.asyncio
    async def test_download_skill_success(self) -> None:
        """Test successful skill download."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = True
        provider._cli_binary = "clawhub"

        with patch.object(provider, "_run_cli_command", return_value="Installed commit-message"):
            result = await provider.download_skill("commit-message")
            assert result.success is True
            assert result.slug == "commit-message"

    @pytest.mark.asyncio
    async def test_download_skill_cli_unavailable(self) -> None:
        """Test download returns error when CLI not available."""
        provider = ClawdHubProvider(
            hub_name="clawdhub",
            base_url="https://clawdhub.com",
        )
        provider._cli_available = False

        result = await provider.download_skill("test-skill")
        assert result.success is False
        assert "not installed" in result.error.lower()


class TestParseSearchText:
    """Tests for the static search text parser."""

    def test_parse_standard_format(self) -> None:
        """Test parsing standard slug vX.Y.Z description format."""
        output = "my-skill  v1.2.3  A great skill for testing"
        results = ClawdHubProvider._parse_search_text(output)
        assert len(results) == 1
        assert results[0]["slug"] == "my-skill"
        assert results[0]["version"] == "1.2.3"
        assert results[0]["description"] == "A great skill for testing"

    def test_parse_multiple_lines(self) -> None:
        """Test parsing multiple result lines."""
        output = "skill-a  v1.0.0  First\nskill-b  v2.0.0  Second"
        results = ClawdHubProvider._parse_search_text(output)
        assert len(results) == 2

    def test_parse_skips_empty_lines(self) -> None:
        """Test parser skips empty lines."""
        output = "\n\nskill-a  v1.0.0  First\n\n"
        results = ClawdHubProvider._parse_search_text(output)
        assert len(results) == 1

    def test_parse_skips_spinner_lines(self) -> None:
        """Test parser skips spinner/status lines."""
        output = "- Searching\nskill-a  v1.0.0  Result"
        results = ClawdHubProvider._parse_search_text(output)
        assert len(results) == 1
        assert results[0]["slug"] == "skill-a"

    def test_parse_empty_output(self) -> None:
        """Test parser handles empty output."""
        assert ClawdHubProvider._parse_search_text("") == []
