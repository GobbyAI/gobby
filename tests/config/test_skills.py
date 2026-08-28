"""
Tests for config/skills.py module.

TDD RED PHASE: Tests for SkillsConfig class.
"""

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

# =============================================================================
# Import Tests
# =============================================================================


class TestSkillsConfigImport:
    """Test that SkillsConfig can be imported."""

    def test_import_from_skills_module(self) -> None:
        """Test importing SkillsConfig from config.skills."""
        from gobby.config.skills import SkillsConfig

        assert SkillsConfig is not None

    def test_import_from_app_module(self) -> None:
        """Test importing SkillsConfig from config.app (re-export)."""
        from gobby.config.app import SkillsConfig

        assert SkillsConfig is not None


# =============================================================================
# SkillsConfig Default Tests
# =============================================================================


class TestSkillsConfigDefaults:
    """Test SkillsConfig default values."""

    def test_default_instantiation(self) -> None:
        """Test SkillsConfig creates with all defaults."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig()
        assert config.inject_core_skills is True
        assert config.core_skills_path is None
        assert config.injection_format == "summary"

    def test_default_inject_core_skills(self) -> None:
        """Test inject_core_skills defaults to True."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig()
        assert config.inject_core_skills is True

    def test_default_core_skills_path(self) -> None:
        """Test core_skills_path defaults to None."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig()
        assert config.core_skills_path is None

    def test_default_injection_format(self) -> None:
        """Test injection_format defaults to 'summary'."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig()
        assert config.injection_format == "summary"

    def test_default_bundled_max_content_size(self) -> None:
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig()

        assert config.bundled_max_content_size == 15_000


# =============================================================================
# SkillsConfig Custom Values Tests
# =============================================================================


class TestSkillsConfigCustom:
    """Test SkillsConfig with custom values."""

    def test_disable_core_skills_injection(self) -> None:
        """Test disabling core skills injection."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig(inject_core_skills=False)
        assert config.inject_core_skills is False

    def test_custom_core_skills_path(self) -> None:
        """Test setting custom core skills path."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig(core_skills_path="/custom/skills/path")
        assert config.core_skills_path == "/custom/skills/path"

    def test_injection_format_full(self) -> None:
        """Test setting injection_format to 'full'."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig(injection_format="full")
        assert config.injection_format == "full"

    def test_injection_format_none(self) -> None:
        """Test setting injection_format to 'none'."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig(injection_format="none")
        assert config.injection_format == "none"

    def test_custom_bundled_max_content_size(self) -> None:
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig(bundled_max_content_size=9_000)

        assert config.bundled_max_content_size == 9_000

    def test_full_configuration(self) -> None:
        """Test setting all configuration values."""
        from gobby.config.skills import SkillsConfig

        config = SkillsConfig(
            inject_core_skills=False,
            core_skills_path="/my/skills",
            injection_format="full",
        )
        assert config.inject_core_skills is False
        assert config.core_skills_path == "/my/skills"
        assert config.injection_format == "full"


# =============================================================================
# SkillsConfig Validation Tests
# =============================================================================


class TestSkillsConfigValidation:
    """Test SkillsConfig validation."""

    def test_injection_format_valid_values(self) -> None:
        """Test that injection_format accepts valid values."""
        from gobby.config.skills import SkillsConfig

        # All valid values should work
        for fmt in ["summary", "full", "none"]:
            config = SkillsConfig(injection_format=fmt)
            assert config.injection_format == fmt

    def test_injection_format_invalid_value(self) -> None:
        """Test that injection_format rejects invalid values."""
        from gobby.config.skills import SkillsConfig

        with pytest.raises(ValidationError):
            SkillsConfig(injection_format="invalid")

    def test_bundled_max_content_size_must_be_positive(self) -> None:
        from gobby.config.skills import SkillsConfig

        with pytest.raises(ValidationError):
            SkillsConfig(bundled_max_content_size=0)


# =============================================================================
# DaemonConfig Integration Tests
# =============================================================================


class TestDaemonConfigSkillsField:
    """Test DaemonConfig.skills field integration."""

    def test_daemon_config_has_skills_field(self) -> None:
        """Test that DaemonConfig has skills field."""
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert hasattr(config, "skills")

    def test_daemon_config_skills_default(self) -> None:
        """Test DaemonConfig.skills has default SkillsConfig."""
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert config.skills is not None
        assert config.skills.inject_core_skills is True
        assert config.skills.injection_format == "summary"

    def test_daemon_config_skills_custom(self) -> None:
        """Test DaemonConfig.skills can be customized."""
        from gobby.config.app import DaemonConfig, SkillsConfig

        config = DaemonConfig(
            skills=SkillsConfig(
                inject_core_skills=False,
                core_skills_path="/custom/path",
                injection_format="full",
            )
        )
        assert config.skills.inject_core_skills is False
        assert config.skills.core_skills_path == "/custom/path"
        assert config.skills.injection_format == "full"
