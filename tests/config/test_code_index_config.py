"""Tests for code-index configuration."""

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


def test_code_index_config_uses_nested_symbol_summary_defaults() -> None:
    config = CodeIndexConfig()

    assert config.sync_worker_projection_timeout_seconds == 300.0
    assert config.symbol_summary.enabled is True
    assert config.symbol_summary.batch_size == 20
    assert config.symbol_summary.profile == FeatureProfile.LOW
    assert config.symbol_summary.max_concurrency == 2
    assert config.symbol_summary.max_tokens == 100
    assert candidate_labels(config.symbol_summary.candidates) == candidate_labels(
        DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW]
    )


def test_code_index_config_accepts_sync_worker_projection_timeout_override() -> None:
    config = CodeIndexConfig.model_validate({"sync_worker_projection_timeout_seconds": 45.5})

    assert config.sync_worker_projection_timeout_seconds == 45.5


def test_code_index_config_still_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CodeIndexConfig.model_validate({"unknown_field": 1})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        pytest.param("sync_worker_interval_seconds", 0, id="zero-sync-interval"),
        pytest.param("sync_worker_interval_seconds", -0.1, id="negative-sync-interval"),
        pytest.param("sync_worker_batch_size", 0, id="zero-sync-batch"),
        pytest.param("sync_worker_batch_size", -1, id="negative-sync-batch"),
    ],
)
def test_code_index_config_rejects_non_positive_worker_limits(
    field_name: str,
    value: int | float,
) -> None:
    with pytest.raises(ValidationError):
        CodeIndexConfig.model_validate({field_name: value})
