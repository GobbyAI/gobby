"""Tests for session-related configuration models."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gobby.config.app import DaemonConfig, load_yaml
from gobby.config.feature_base import FeatureProfile, candidate_labels
from gobby.config.sessions import MemoryRecallConfig

pytestmark = pytest.mark.unit


def test_memory_recall_config_fields() -> None:
    """Memory recall config exposes only search controls; the classifier is gone."""
    assert set(MemoryRecallConfig.model_fields) == {
        "profile",
        "candidates",
        "enabled",
        "candidate_limit",
        "min_score",
        "selection_min_score",
    }


def test_memory_recall_config_defaults() -> None:
    cfg = MemoryRecallConfig()
    assert cfg.enabled is True
    assert cfg.profile == FeatureProfile.LOW
    assert "claude/haiku" in candidate_labels(cfg.candidates)
    assert cfg.candidate_limit == 8
    assert cfg.min_score == 0.55
    # The selection floor sits above the search floor on purpose: the backfill
    # loop chases min_score until the candidate pool fills, so only a floor it
    # does not chase can make a turn inject less than the rank limit.
    assert cfg.selection_min_score == 0.65
    assert cfg.selection_min_score > cfg.min_score
    assert DaemonConfig().memory_recall.enabled is True


def _assert_field_error(exc: ValidationError, field: str) -> None:
    assert any(error["loc"] == (field,) for error in exc.errors())


@pytest.mark.parametrize("field", ["min_score", "selection_min_score"])
def test_memory_recall_config_validation_bounds(field: str) -> None:
    assert getattr(MemoryRecallConfig(**{field: 0.75}), field) == 0.75
    with pytest.raises(ValidationError) as exc_info:
        MemoryRecallConfig(**{field: -0.1})
    _assert_field_error(exc_info.value, field)
    with pytest.raises(ValidationError) as exc_info:
        MemoryRecallConfig(**{field: 1.1})
    _assert_field_error(exc_info.value, field)


def test_memory_recall_config_yaml_loading(temp_dir: Path) -> None:
    disabled_config_file = temp_dir / "disabled.yaml"
    disabled_config_file.write_text(
        yaml.safe_dump(
            {
                "memory_recall": {
                    "enabled": False,
                    "candidates": ["endpoint:lm-studio/llama"],
                    "candidate_limit": 5,
                    "min_score": 0.75,
                    "selection_min_score": 0.85,
                }
            }
        )
    )
    disabled_config = DaemonConfig(**load_yaml(str(disabled_config_file)))
    assert disabled_config.memory_recall.enabled is False
    assert candidate_labels(disabled_config.memory_recall.candidates) == (
        "endpoint:lm-studio/llama",
    )
    assert disabled_config.memory_recall.candidate_limit == 5
    assert disabled_config.memory_recall.min_score == 0.75
    assert disabled_config.memory_recall.selection_min_score == 0.85

    default_config_file = temp_dir / "default.yaml"
    default_config_file.write_text(yaml.safe_dump({"daemon_port": 60999}))
    default_config = DaemonConfig(**load_yaml(str(default_config_file)))
    assert default_config.memory_recall.enabled is True


def test_removed_memory_recall_helper_config_fails_loud(temp_dir: Path) -> None:
    """Stale helper config should be visible at startup."""
    config_file = temp_dir / "stale-helper.yaml"
    config_file.write_text(yaml.safe_dump({"memory_recall_helper": {"enabled": False}}))

    with pytest.raises(ValueError, match="memory_recall_helper config has been removed"):
        DaemonConfig(**load_yaml(str(config_file)))


def test_daemon_config_validates_without_a_recall_timeout() -> None:
    """1.1.3: the load-order validator lost its first term and must still hold."""
    assert "timeout" not in MemoryRecallConfig.model_fields
    config = DaemonConfig()
    assert config.workflow.timeout < config.hooks.adapter_timeout
    assert config.hooks.adapter_timeout < config.hooks.provider_timeout
