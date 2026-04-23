"""Tests for FeatureDefaultConfig, ModelTier, and tier fallback mapping."""

from __future__ import annotations

import pytest

from gobby.config.feature_base import (
    TIER_FALLBACK_MODEL,
    FeatureDefaultConfig,
    ModelTier,
)

pytestmark = pytest.mark.unit


class TestModelTier:
    def test_enum_values(self) -> None:
        assert ModelTier.LOW == "low"
        assert ModelTier.MID == "mid"
        assert ModelTier.HIGH == "high"

    def test_is_str_enum(self) -> None:
        assert isinstance(ModelTier.LOW, str)

    def test_all_tiers_have_fallback(self) -> None:
        for tier in ModelTier:
            assert tier in TIER_FALLBACK_MODEL


class TestTierFallbackModel:
    def test_low_maps_to_haiku(self) -> None:
        assert TIER_FALLBACK_MODEL[ModelTier.LOW] == "haiku"

    def test_mid_maps_to_sonnet(self) -> None:
        assert TIER_FALLBACK_MODEL[ModelTier.MID] == "sonnet"

    def test_high_maps_to_opus(self) -> None:
        assert TIER_FALLBACK_MODEL[ModelTier.HIGH] == "opus"


class TestFeatureDefaultConfig:
    def test_defaults(self) -> None:
        cfg = FeatureDefaultConfig()
        assert cfg.provider == "claude"
        assert cfg.model == "haiku"
        assert cfg.tier == ModelTier.LOW

    def test_override(self) -> None:
        cfg = FeatureDefaultConfig(provider="local", model="qwen2.5", tier=ModelTier.MID)
        assert cfg.provider == "local"
        assert cfg.model == "qwen2.5"
        assert cfg.tier == ModelTier.MID


class TestFeatureConfigInheritance:
    """Verify that production feature configs inherit from FeatureDefaultConfig."""

    def test_digest_config(self) -> None:
        from gobby.config.sessions import DigestConfig

        assert issubclass(DigestConfig, FeatureDefaultConfig)
        cfg = DigestConfig()
        assert cfg.tier == ModelTier.LOW
        assert cfg.model == "haiku"

    def test_session_summary_config(self) -> None:
        from gobby.config.sessions import SessionSummaryConfig

        assert issubclass(SessionSummaryConfig, FeatureDefaultConfig)
        cfg = SessionSummaryConfig()
        assert cfg.tier == ModelTier.MID
        assert cfg.model == "sonnet"

    def test_recommend_tools_config(self) -> None:
        from gobby.config.features import RecommendToolsConfig

        assert issubclass(RecommendToolsConfig, FeatureDefaultConfig)
        cfg = RecommendToolsConfig()
        assert cfg.tier == ModelTier.MID
        assert cfg.model == "sonnet"

    def test_task_expansion_config(self) -> None:
        from gobby.config.tasks import TaskExpansionConfig

        assert issubclass(TaskExpansionConfig, FeatureDefaultConfig)
        cfg = TaskExpansionConfig()
        assert cfg.tier == ModelTier.HIGH
        assert cfg.model == "opus"

    def test_task_validation_config(self) -> None:
        from gobby.config.tasks import TaskValidationConfig

        assert issubclass(TaskValidationConfig, FeatureDefaultConfig)
        cfg = TaskValidationConfig()
        assert cfg.tier == ModelTier.MID
        assert cfg.model == "sonnet"

    def test_tool_summarizer_config(self) -> None:
        from gobby.config.features import ToolSummarizerConfig

        assert issubclass(ToolSummarizerConfig, FeatureDefaultConfig)
        cfg = ToolSummarizerConfig()
        assert cfg.tier == ModelTier.LOW
        assert cfg.model == "haiku"

    def test_conductor_config(self) -> None:
        from gobby.config.conductor import ConductorConfig

        assert issubclass(ConductorConfig, FeatureDefaultConfig)
        cfg = ConductorConfig()
        assert cfg.tier == ModelTier.LOW
        assert cfg.model == "haiku"

    def test_merge_resolution_config(self) -> None:
        from gobby.config.features import MergeResolutionConfig

        assert issubclass(MergeResolutionConfig, FeatureDefaultConfig)
        cfg = MergeResolutionConfig()
        assert cfg.tier == ModelTier.MID
        assert cfg.model == "sonnet"
