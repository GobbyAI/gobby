"""Tests for feature profile routing config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.config.feature_base import (
    DEFAULT_PROFILE_CANDIDATES,
    FeatureDefaultConfig,
    FeatureProfile,
)

pytestmark = pytest.mark.unit


class TestFeatureProfile:
    def test_enum_values(self) -> None:
        assert FeatureProfile.LOW == "feature_low"
        assert FeatureProfile.MID == "feature_mid"
        assert FeatureProfile.HIGH == "feature_high"

    def test_is_str_enum(self) -> None:
        assert isinstance(FeatureProfile.LOW, str)

    def test_all_profiles_have_candidates(self) -> None:
        for profile in FeatureProfile:
            assert DEFAULT_PROFILE_CANDIDATES[profile]


class TestFeatureDefaultConfig:
    def test_defaults(self) -> None:
        cfg = FeatureDefaultConfig()
        assert cfg.profile == FeatureProfile.LOW
        assert cfg.candidates == list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW])

    def test_profile_fills_matching_candidates(self) -> None:
        cfg = FeatureDefaultConfig(profile=FeatureProfile.MID)
        assert cfg.candidates == list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.MID])

    def test_custom_candidates(self) -> None:
        cfg = FeatureDefaultConfig(
            profile=FeatureProfile.HIGH,
            candidates=["qwen/qwen3-coder", "claude/opus"],
        )
        assert cfg.candidates == ["qwen/qwen3-coder", "claude/opus"]

    @pytest.mark.parametrize("old_key", ["provider", "model", "tier"])
    def test_rejects_removed_feature_keys(self, old_key: str) -> None:
        with pytest.raises(ValidationError):
            FeatureDefaultConfig(**{old_key: "claude"})  # type: ignore[arg-type]

    def test_rejects_unscoped_candidate(self) -> None:
        with pytest.raises(ValidationError, match="provider/model"):
            FeatureDefaultConfig(candidates=["haiku"])


class TestFeatureConfigInheritance:
    """Verify production feature configs use profile defaults."""

    def test_low_feature_configs(self) -> None:
        from gobby.config.features import ImportMCPServerConfig, ToolSummarizerConfig
        from gobby.config.persistence import MemoryKnowledgeGraphConfig, MemoryStaleAuditConfig
        from gobby.config.sessions import DigestConfig, MemoryRecallConfig, SessionSummaryConfig

        for config_cls in (
            DigestConfig,
            ImportMCPServerConfig,
            MemoryKnowledgeGraphConfig,
            MemoryRecallConfig,
            MemoryStaleAuditConfig,
            SessionSummaryConfig,
            ToolSummarizerConfig,
        ):
            cfg = config_cls()
            assert cfg.profile == FeatureProfile.LOW
            assert cfg.candidates == list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW])

    def test_mid_feature_configs(self) -> None:
        from gobby.config.features import MergeResolutionConfig, RecommendToolsConfig
        from gobby.config.tasks import TaskValidationConfig

        for config_cls in (MergeResolutionConfig, RecommendToolsConfig, TaskValidationConfig):
            cfg = config_cls()
            assert cfg.profile == FeatureProfile.MID
            assert cfg.candidates == list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.MID])

    def test_high_feature_configs(self) -> None:
        from gobby.config.features import ChatConfig
        from gobby.config.tasks import TaskExpansionConfig

        for config_cls in (ChatConfig, TaskExpansionConfig):
            cfg = config_cls()
            assert cfg.profile == FeatureProfile.HIGH
            assert cfg.candidates == list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.HIGH])
