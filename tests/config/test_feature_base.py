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

    def test_profile_candidate_ordering(self) -> None:
        assert DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW] == (
            "codex/gpt-5.3-codex-spark",
            "codex/gpt-5.4-mini",
            "claude/haiku",
            "local/Qwen3-Coder-30B-A3B-Instruct",
        )
        assert DEFAULT_PROFILE_CANDIDATES[FeatureProfile.MID] == (
            "codex/gpt-5.3-codex-spark",
            "claude/sonnet",
            "local/Qwen3-Coder-Next",
        )
        assert DEFAULT_PROFILE_CANDIDATES[FeatureProfile.HIGH] == (
            "codex/gpt-5.3-codex",
            "claude/opus",
            "local/Qwen3-Coder-Next",
        )


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

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ("claude/claude-haiku-4-5", "claude/haiku"),
            ("claude/claude-haiku-4-5-20251001", "claude/haiku"),
            ("claude/claude-sonnet-4-5", "claude/sonnet"),
            ("claude/claude-opus-4-1", "claude/opus"),
            ("codex/claude-haiku-4-5", "codex/claude-haiku-4-5"),
        ],
    )
    def test_normalizes_claude_family_candidate_labels(self, candidate: str, expected: str) -> None:
        cfg = FeatureDefaultConfig(candidates=[candidate])
        assert cfg.candidates == [expected]

    def test_migrates_legacy_feature_keys(self) -> None:
        cfg = FeatureDefaultConfig(
            **{"provider": "claude", "model": "claude-sonnet-4-5", "tier": "high"}
        )

        assert cfg.profile == FeatureProfile.HIGH
        assert cfg.candidates[0] == "claude/sonnet"

    @pytest.mark.parametrize("candidate", ["haiku", "claude/", "/sonnet"])
    def test_rejects_malformed_candidate(self, candidate: str) -> None:
        with pytest.raises(ValidationError, match="provider/model"):
            FeatureDefaultConfig(candidates=[candidate])


class TestFeatureConfigInheritance:
    """Verify production feature configs use profile defaults."""

    def test_low_feature_configs(self) -> None:
        from gobby.config.features import ImportMCPServerConfig, ToolSummarizerConfig
        from gobby.config.persistence import MemoryKnowledgeGraphConfig
        from gobby.config.sessions import DigestConfig, MemoryRecallConfig, SessionSummaryConfig

        for config_cls in (
            DigestConfig,
            ImportMCPServerConfig,
            MemoryKnowledgeGraphConfig,
            MemoryRecallConfig,
            SessionSummaryConfig,
            ToolSummarizerConfig,
        ):
            cfg = config_cls()
            assert cfg.profile == FeatureProfile.LOW
            assert cfg.candidates == list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW])

    def test_mid_feature_configs(self) -> None:
        from gobby.config.features import MergeResolutionConfig, RecommendToolsConfig
        from gobby.config.persistence import MemoryDreamConfig
        from gobby.config.tasks import TaskValidationConfig

        for config_cls in (
            MemoryDreamConfig,
            MergeResolutionConfig,
            RecommendToolsConfig,
            TaskValidationConfig,
        ):
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
