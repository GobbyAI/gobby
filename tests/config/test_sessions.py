"""Tests for session-related configuration models."""

from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig, load_yaml
from gobby.config.feature_base import FeatureProfile
from gobby.config.sessions import MemoryRecallConfig

pytestmark = pytest.mark.unit


def test_memory_recall_config_shape(temp_dir: Path) -> None:
    """Memory recall config exposes daemon-owned runner controls."""
    assert set(MemoryRecallConfig.model_fields) == {
        "profile",
        "candidates",
        "enabled",
        "timeout",
        "candidate_limit",
        "selected_limit",
        "min_score",
        "query_synthesis_threshold",
        "query_max_chars",
    }
    cfg = MemoryRecallConfig()
    assert cfg.enabled is True
    assert cfg.profile == FeatureProfile.LOW
    assert "claude/haiku" in cfg.candidates
    assert cfg.timeout == 60
    assert cfg.candidate_limit == 8
    assert cfg.selected_limit == 3
    assert cfg.min_score == 0.7
    assert cfg.query_synthesis_threshold == 8_000
    assert cfg.query_max_chars == 1_200
    assert DaemonConfig().memory_recall.enabled is True
    assert MemoryRecallConfig(min_score=0.75).min_score == 0.75
    with pytest.raises(ValueError):
        MemoryRecallConfig(min_score=0.69)

    disabled_config_file = temp_dir / "disabled.yaml"
    disabled_config_file.write_text(
        yaml.safe_dump(
            {
                "memory_recall": {
                    "enabled": False,
                    "candidates": ["local:lm-studio/llama"],
                    "timeout": 12,
                    "candidate_limit": 5,
                    "selected_limit": 2,
                    "min_score": 0.75,
                    "query_synthesis_threshold": 100,
                    "query_max_chars": 80,
                }
            }
        )
    )
    disabled_config = DaemonConfig(**load_yaml(str(disabled_config_file)))
    assert disabled_config.memory_recall.enabled is False
    assert disabled_config.memory_recall.candidates == ["local:lm-studio/llama"]
    assert disabled_config.memory_recall.timeout == 12
    assert disabled_config.memory_recall.candidate_limit == 5
    assert disabled_config.memory_recall.selected_limit == 2
    assert disabled_config.memory_recall.min_score == 0.75
    assert disabled_config.memory_recall.query_synthesis_threshold == 100
    assert disabled_config.memory_recall.query_max_chars == 80

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
