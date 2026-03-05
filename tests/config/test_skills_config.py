import pytest
from pydantic import ValidationError

from gobby.config.skills import HubConfig, SkillsConfig

pytestmark = pytest.mark.unit


class TestHubConfig:
    def test_hub_config_valid_clawdhub(self) -> None:
        """Test that 'clawdhub' is a valid type."""
        config = HubConfig(type="clawdhub")
        assert config.type == "clawdhub"

    def test_hub_config_valid_skillsmp(self) -> None:
        """Test that 'skillsmp' is a valid type."""
        config = HubConfig(type="skillsmp")
        assert config.type == "skillsmp"

    def test_hub_config_valid_github_collection(self) -> None:
        """Test that 'github-collection' is a valid type."""
        config = HubConfig(type="github-collection")
        assert config.type == "github-collection"

    def test_hub_config_invalid_type(self) -> None:
        """Test that invalid types raise ValidationError."""
        with pytest.raises(ValidationError) as excinfo:
            HubConfig(type="invalid-type")
        # Pydantic's error message for Literal validation failure - includes all valid types
        assert "claude-plugins" in str(excinfo.value)

    def test_hub_config_optional_fields(self) -> None:
        """Test that optional fields are correctly set."""
        config = HubConfig(
            type="skillsmp",
            base_url="https://example.com",
            repo="user/repo",
            branch="main",
            auth_key_name="MY_KEY",
        )
        assert config.base_url == "https://example.com"
        assert config.repo == "user/repo"
        assert config.branch == "main"
        assert config.auth_key_name == "MY_KEY"

    def test_hub_config_defaults(self) -> None:
        """Test that optional fields default to None."""
        config = HubConfig(type="skillsmp")
        assert config.base_url is None
        assert config.repo is None
        assert config.branch is None
        assert config.auth_key_name is None

    def test_hub_config_required_type(self) -> None:
        """Test that 'type' is required."""
        with pytest.raises(ValidationError) as excinfo:
            HubConfig()
        assert "Field required" in str(excinfo.value)


class TestSkillsConfigHubs:
    """Tests for SkillsConfig.hubs field."""

    def test_hubs_empty_default(self) -> None:
        """Test that hubs defaults to built-in hubs."""
        config = SkillsConfig()
        assert "anthropic-skills" in config.hubs
        assert "claude-plugins" in config.hubs
        assert "clawdhub" in config.hubs
        assert "skillsmp" in config.hubs
        assert len(config.hubs) == 4
        assert config.hubs["clawdhub"].type == "clawdhub"
        assert config.hubs["skillsmp"].type == "skillsmp"
        assert config.hubs["skillsmp"].base_url == "https://skillsmp.com/api/v1"
        assert isinstance(config.hubs, dict)

    def test_hubs_single_hub(self) -> None:
        """Test parsing a single hub config."""
        config = SkillsConfig(
            hubs={"clawdhub": HubConfig(type="clawdhub", base_url="https://clawdhub.com")}
        )
        assert len(config.hubs) == 1
        assert "clawdhub" in config.hubs
        assert config.hubs["clawdhub"].type == "clawdhub"
        assert config.hubs["clawdhub"].base_url == "https://clawdhub.com"

    def test_hubs_multiple_hubs(self) -> None:
        """Test parsing multiple hub configs."""
        config = SkillsConfig(
            hubs={
                "clawdhub": HubConfig(type="clawdhub", base_url="https://clawdhub.com"),
                "skillsmp": HubConfig(type="skillsmp", auth_key_name="SKILLHUB_KEY"),
                "my-collection": HubConfig(
                    type="github-collection",
                    repo="user/my-skills",
                    branch="main",
                ),
            }
        )
        assert len(config.hubs) == 3
        assert config.hubs["clawdhub"].type == "clawdhub"
        assert config.hubs["skillsmp"].type == "skillsmp"
        assert config.hubs["skillsmp"].auth_key_name == "SKILLHUB_KEY"
        assert config.hubs["my-collection"].type == "github-collection"
        assert config.hubs["my-collection"].repo == "user/my-skills"

    def test_hubs_from_dict(self) -> None:
        """Test parsing hubs from raw dict (simulating YAML input)."""
        raw_config = {
            "hubs": {
                "clawdhub": {"type": "clawdhub", "base_url": "https://clawdhub.com"},
                "skillsmp": {"type": "skillsmp"},
            }
        }
        config = SkillsConfig(**raw_config)
        assert len(config.hubs) == 2
        assert config.hubs["clawdhub"].type == "clawdhub"
        assert config.hubs["skillsmp"].type == "skillsmp"

    def test_hubs_invalid_hub_config(self) -> None:
        """Test that invalid hub config raises ValidationError."""
        with pytest.raises(ValidationError):
            SkillsConfig(
                hubs={
                    "bad": {"type": "invalid-type"}  # Invalid type
                }
            )
