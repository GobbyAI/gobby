"""Tests for code-index configuration compatibility."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.config.code_index import CodeIndexConfig
from gobby.config.feature_base import (
    DEFAULT_PROFILE_CANDIDATES,
    FeatureProfile,
    candidate_labels,
)

pytestmark = pytest.mark.unit


def test_code_index_config_drops_deprecated_vector_batch_size() -> None:
    config = CodeIndexConfig.model_validate(
        {
            "sync_worker_vector_batch_size": 12,
            "sync_worker_batch_size": 3,
        }
    )

    assert config.sync_worker_batch_size == 3
    assert "sync_worker_vector_batch_size" not in config.model_dump()


def test_code_index_config_uses_nested_symbol_summary_defaults() -> None:
    config = CodeIndexConfig()

    assert config.symbol_summary.enabled is True
    assert config.symbol_summary.batch_size == 20
    assert config.symbol_summary.profile == FeatureProfile.LOW
    assert config.symbol_summary.max_concurrency == 2
    assert config.symbol_summary.max_tokens == 100
    assert candidate_labels(config.symbol_summary.candidates) == candidate_labels(
        DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW]
    )


def test_code_index_config_migrates_legacy_flat_summary_fields() -> None:
    config = CodeIndexConfig.model_validate(
        {
            "summary_enabled": False,
            "summary_batch_size": 7,
            "summary_profile": "feature_mid",
            "summary_candidates": ["claude/sonnet"],
            "summary_max_concurrency": 3,
        }
    )

    assert config.symbol_summary.enabled is False
    assert config.symbol_summary.batch_size == 7
    assert config.symbol_summary.profile == FeatureProfile.MID
    assert candidate_labels(config.symbol_summary.candidates) == ("claude/sonnet",)
    assert config.symbol_summary.max_concurrency == 3


def test_code_index_config_still_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CodeIndexConfig.model_validate({"unknown_field": 1})
