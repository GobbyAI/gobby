"""Tests for session-related configuration models."""

from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig, load_yaml
from gobby.config.feature_base import FeatureProfile, candidate_labels
from gobby.config.sessions import MemoryRecallConfig

pytestmark = pytest.mark.unit


def test_memory_recall_config_shape(temp_dir: Path) -> None:
    """Memory recall config exposes only classifier and search controls."""
    assert set(MemoryRecallConfig.model_fields) == {
        "profile",
        "candidates",
        "enabled",
        "timeout",
        "candidate_limit",
        "min_score",
    }
    cfg = MemoryRecallConfig()
    assert cfg.enabled is True
    assert cfg.profile == FeatureProfile.LOW
    assert "claude/haiku" in candidate_labels(cfg.candidates)
    assert cfg.timeout == 60
    assert cfg.candidate_limit == 8
    assert cfg.min_score == 0.45
    assert DaemonConfig().memory_recall.enabled is True
    assert MemoryRecallConfig(min_score=0.75).min_score == 0.75
    with pytest.raises(ValueError):
        MemoryRecallConfig(min_score=-0.1)
    with pytest.raises(ValueError):
        MemoryRecallConfig(min_score=1.1)
    with pytest.raises(ValueError):
        MemoryRecallConfig(timeout=0)
    assert MemoryRecallConfig(timeout=75).timeout == 75
    assert MemoryRecallConfig(timeout=89).timeout == 89
    with pytest.raises(ValueError):
        MemoryRecallConfig(timeout=90)

    disabled_config_file = temp_dir / "disabled.yaml"
    disabled_config_file.write_text(
        yaml.safe_dump(
            {
                "memory_recall": {
                    "enabled": False,
                    "candidates": ["endpoint:lm-studio/llama"],
                    "timeout": 12,
                    "candidate_limit": 5,
                    "min_score": 0.75,
                }
            }
        )
    )
    disabled_config = DaemonConfig(**load_yaml(str(disabled_config_file)))
    assert disabled_config.memory_recall.enabled is False
    assert candidate_labels(disabled_config.memory_recall.candidates) == (
        "endpoint:lm-studio/llama",
    )
    assert disabled_config.memory_recall.timeout == 12
    assert disabled_config.memory_recall.candidate_limit == 5
    assert disabled_config.memory_recall.min_score == 0.75

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
