"""Tests for feature profile routing config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.feature_base import (
    DEFAULT_PROFILE_CANDIDATES,
    FeatureCandidateConfig,
    FeatureDefaultConfig,
    FeatureProfile,
    candidate_labels,
    candidate_runtime_entries,
    default_candidates_for_profile,
    default_reasoning_for_profile,
    parse_feature_candidate,
)

pytestmark = pytest.mark.unit


class TestFeatureProfile:
    def test_enum_values(self) -> None:
        assert FeatureProfile.LOW.value == "feature_low"
        assert FeatureProfile.MID.value == "feature_mid"
        assert FeatureProfile.HIGH.value == "feature_high"

    def test_is_str_enum(self) -> None:
        assert isinstance(FeatureProfile.LOW, str)

    def test_all_profiles_have_candidates(self) -> None:
        for profile in FeatureProfile:
            assert DEFAULT_PROFILE_CANDIDATES[profile]

    def test_profile_candidate_ordering(self) -> None:
        assert candidate_labels(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW]) == (
            "codex/gpt-5.6-luna",
            "claude/haiku",
        )
        assert candidate_labels(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.MID]) == (
            "codex/gpt-5.6-terra",
            "claude/sonnet",
        )
        assert candidate_labels(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.HIGH]) == (
            "codex/gpt-5.6-sol",
            "claude/opus",
        )
        for candidates in DEFAULT_PROFILE_CANDIDATES.values():
            assert "claude/fable" not in candidate_labels(candidates)

    def test_profile_candidate_reasoning_pins(self) -> None:
        high_candidates = DEFAULT_PROFILE_CANDIDATES[FeatureProfile.HIGH]

        assert [candidate.reasoning_effort for candidate in high_candidates] == [
            "xhigh",
            "high",
        ]
        for profile in (FeatureProfile.LOW, FeatureProfile.MID):
            assert all(
                candidate.reasoning_effort is None
                for candidate in DEFAULT_PROFILE_CANDIDATES[profile]
            )

    def test_default_candidates_for_profile_returns_labels(self) -> None:
        assert default_candidates_for_profile(FeatureProfile.HIGH) == (
            "codex/gpt-5.6-sol",
            "claude/opus",
        )

    def test_default_reasoning_for_profile_is_auto_unset(self) -> None:
        assert default_reasoning_for_profile(FeatureProfile.LOW) == "auto"
        assert default_reasoning_for_profile(FeatureProfile.MID) is None
        assert default_reasoning_for_profile(FeatureProfile.HIGH) is None

    def test_profiles_use_cloud_only_candidates(self) -> None:
        for candidates in DEFAULT_PROFILE_CANDIDATES.values():
            providers = {candidate.split("/", 1)[0] for candidate in candidate_labels(candidates)}
            assert providers <= {"codex", "claude"}


class TestFeatureDefaultConfig:
    def test_defaults(self) -> None:
        cfg = FeatureDefaultConfig()
        assert cfg.profile == FeatureProfile.LOW
        assert cfg._candidates_omitted is True
        assert candidate_labels(cfg.candidates) == default_candidates_for_profile(
            FeatureProfile.LOW
        )

    def test_profile_fills_matching_candidates(self) -> None:
        cfg = FeatureDefaultConfig(profile=FeatureProfile.MID)
        assert candidate_labels(cfg.candidates) == default_candidates_for_profile(
            FeatureProfile.MID
        )

    def test_custom_candidates(self) -> None:
        cfg = FeatureDefaultConfig(
            profile=FeatureProfile.HIGH,
            candidates=["qwen/qwen3-coder", "claude/opus"],
        )
        assert cfg._candidates_omitted is False
        assert candidate_labels(cfg.candidates) == ("qwen/qwen3-coder", "claude/opus")

    def test_explicit_empty_candidates_remain_empty(self) -> None:
        cfg = FeatureDefaultConfig(profile=FeatureProfile.HIGH, candidates=[])

        assert cfg._candidates_omitted is False
        assert cfg.candidates == []

    def test_structured_candidate_config_parses_reasoning_effort(self) -> None:
        cfg = FeatureDefaultConfig(
            candidates=[
                {"candidate": "codex/gpt-5.6-sol", "reasoning_effort": "xhigh"},
                FeatureCandidateConfig(candidate="claude/opus", reasoning_effort="HIGH"),
            ],
        )

        assert candidate_labels(cfg.candidates) == ("codex/gpt-5.6-sol", "claude/opus")
        first_candidate, second_candidate = cfg.candidates
        assert isinstance(first_candidate, FeatureCandidateConfig)
        assert isinstance(second_candidate, FeatureCandidateConfig)
        assert [first_candidate.reasoning_effort, second_candidate.reasoning_effort] == [
            "xhigh",
            "high",
        ]

    @pytest.mark.parametrize(
        ("reasoning_effort", "expected"),
        [("auto", "auto"), ("", None), ("  AUTO  ", "auto"), (None, None)],
    )
    def test_reasoning_effort_preserves_auto_and_unset(
        self,
        reasoning_effort: str | None,
        expected: str | None,
    ) -> None:
        cfg = FeatureDefaultConfig(
            candidates=[
                {"candidate": "codex/gpt-5.6-terra", "reasoning_effort": reasoning_effort},
            ],
        )

        candidate = cfg.candidates[0]
        assert isinstance(candidate, FeatureCandidateConfig)
        assert candidate.reasoning_effort == expected

    def test_unknown_reasoning_effort_is_accepted_at_config_load(self) -> None:
        cfg = FeatureDefaultConfig(
            candidates=[
                {"candidate": "codex/gpt-5.6-terra", "reasoning_effort": "banana"},
            ],
        )

        candidate = cfg.candidates[0]
        assert isinstance(candidate, FeatureCandidateConfig)
        assert candidate.reasoning_effort == "banana"

    def test_explicit_candidate_reasoning_overrides_profile_default(self) -> None:
        entries = candidate_runtime_entries(
            [{"candidate": "codex/gpt-5.6-sol", "reasoning_effort": "xhigh"}],
            profile=FeatureProfile.HIGH,
        )

        assert entries[0].reasoning_effort == "xhigh"

    def test_candidate_runtime_entries_preserve_auto_profile_default(self) -> None:
        entries = candidate_runtime_entries(
            [{"candidate": "codex/gpt-5.6-terra", "reasoning_effort": "auto"}],
            profile=FeatureProfile.HIGH,
        )

        assert entries[0].reasoning_effort == "auto"

    def test_deduplicates_normalized_candidates_preserving_order(self) -> None:
        cfg = FeatureDefaultConfig(
            candidates=[
                "claude/claude-haiku-4-5",
                "codex/gpt-5.6-terra",
                "claude/haiku",
            ],
        )

        assert candidate_labels(cfg.candidates) == ("claude/haiku", "codex/gpt-5.6-terra")

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ("claude/claude-haiku-4-5", "claude/haiku"),
            ("claude/claude-haiku-4-5-20251001", "claude/haiku"),
            ("claude/claude-sonnet-4-5", "claude/sonnet"),
            ("claude/claude-opus-4-1", "claude/claude-opus-4-1"),
            ("claude/claude-opus-4-5", "claude/claude-opus-4-5"),
            ("claude/fable", "claude/fable"),
            ("claude/claude-fable-5", "claude/claude-fable-5"),
            ("codex/claude-haiku-4-5", "codex/claude-haiku-4-5"),
        ],
    )
    def test_normalizes_claude_family_candidate_labels(self, candidate: str, expected: str) -> None:
        cfg = FeatureDefaultConfig(candidates=[candidate])
        assert candidate_labels(cfg.candidates) == (expected,)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"provider": "claude"},
            {"model": "claude-sonnet-4-5"},
            {"tier": "high"},
            {"provider": "claude", "model": "claude-sonnet-4-5", "tier": "high"},
        ],
    )
    def test_rejects_removed_legacy_feature_keys(self, kwargs: dict[str, str]) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            FeatureDefaultConfig(**kwargs)

    @pytest.mark.parametrize("candidate", ["haiku", "claude/", "/sonnet"])
    def test_rejects_malformed_candidate(self, candidate: str) -> None:
        with pytest.raises(ValidationError, match="provider/model"):
            FeatureDefaultConfig(candidates=[candidate])

    def test_parses_named_local_candidate_with_slashed_model_id(self) -> None:
        assert parse_feature_candidate("endpoint:lm-studio/google/gemma-4-26b-a4b-qat") == (
            "endpoint:lm-studio",
            "google/gemma-4-26b-a4b-qat",
        )

    def test_rejects_removed_local_generation_selector(self) -> None:
        with pytest.raises(ValueError, match=r"replace it with endpoint:\*"):
            parse_feature_candidate("local:lm-studio/google/gemma")


class TestGenerationProfileDefaults:
    def test_daemon_config_applies_profile_defaults_when_candidates_omitted(self) -> None:
        config = DaemonConfig(
            ai={
                "generation": {
                    "profile_defaults": {
                        "feature_low": [
                            "codex/gpt-5.4-mini",
                            "claude/haiku",
                            "endpoint:lm-studio/google/gemma-4-26b-a4b-qat",
                        ],
                    }
                }
            }
        )

        assert candidate_labels(config.session_summary.candidates) == (
            "codex/gpt-5.4-mini",
            "claude/haiku",
            "endpoint:lm-studio/google/gemma-4-26b-a4b-qat",
        )

    def test_daemon_config_keeps_explicit_feature_candidates_authoritative(self) -> None:
        config = DaemonConfig(
            session_summary={
                "candidates": ["codex/gpt-5.4-mini"],
            },
            ai={
                "generation": {
                    "profile_defaults": {
                        "feature_low": [
                            "codex/gpt-5.4-mini",
                            "claude/haiku",
                            "endpoint:lm-studio/google/gemma-4-26b-a4b-qat",
                        ],
                    }
                }
            },
        )

        assert candidate_labels(config.session_summary.candidates) == ("codex/gpt-5.4-mini",)

    def test_candidates_equal_to_static_default_follow_profile_defaults(self) -> None:
        """Materialized residue equal to the baked default must not pin (#20689)."""
        config = DaemonConfig(
            session_summary={
                "candidates": [
                    {"candidate": "codex/gpt-5.6-luna"},
                    {"candidate": "claude/haiku"},
                ],
            },
            ai={
                "generation": {
                    "profile_defaults": {
                        "feature_low": ["endpoint:lm-studio/google/gemma-4-26b-a4b-qat"],
                    }
                }
            },
        )

        assert candidate_labels(config.session_summary.candidates) == (
            "endpoint:lm-studio/google/gemma-4-26b-a4b-qat",
        )

    def test_candidates_equal_to_static_default_stay_default_without_profile_defaults(
        self,
    ) -> None:
        config = DaemonConfig(
            session_summary={
                "candidates": [
                    {"candidate": "codex/gpt-5.6-luna"},
                    {"candidate": "claude/haiku"},
                ],
            },
        )

        assert candidate_labels(config.session_summary.candidates) == (
            "codex/gpt-5.6-luna",
            "claude/haiku",
        )


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
            assert candidate_labels(cfg.candidates) == default_candidates_for_profile(
                FeatureProfile.LOW
            )

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
            assert candidate_labels(cfg.candidates) == default_candidates_for_profile(
                FeatureProfile.MID
            )

    def test_high_feature_configs(self) -> None:
        from gobby.config.features import ChatConfig
        from gobby.config.tasks import TaskExpansionConfig

        for config_cls in (ChatConfig, TaskExpansionConfig):
            cfg = config_cls()
            assert cfg.profile == FeatureProfile.HIGH
            assert candidate_labels(cfg.candidates) == default_candidates_for_profile(
                FeatureProfile.HIGH
            )

    def test_memory_dream_work_unit_timeout_must_be_positive(self) -> None:
        from gobby.config.persistence import MemoryDreamConfig

        assert MemoryDreamConfig().work_unit_timeout_seconds == 1500.0
        with pytest.raises(ValidationError, match="greater than 0"):
            MemoryDreamConfig(work_unit_timeout_seconds=0.0)
