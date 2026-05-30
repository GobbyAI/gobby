"""Tests for session-related configuration models."""

from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig, load_yaml
from gobby.config.sessions import MemoryRecallConfig

pytestmark = pytest.mark.unit


def test_memory_recall_config_shape(temp_dir: Path) -> None:
    """Memory recall config exposes daemon-owned runner controls."""
    assert set(MemoryRecallConfig.model_fields) == {
        "provider",
        "model",
        "tier",
        "enabled",
        "timeout",
        "candidate_limit",
        "selected_limit",
        "min_score",
    }
    cfg = MemoryRecallConfig()
    assert cfg.enabled is True
    assert cfg.provider == "claude"
    assert cfg.model == "haiku"
    assert cfg.tier == "low"
    assert cfg.timeout == 60
    assert cfg.candidate_limit == 8
    assert cfg.selected_limit == 3
    assert cfg.min_score == 0.5
    assert DaemonConfig().memory_recall.enabled is True

    disabled_config_file = temp_dir / "disabled.yaml"
    disabled_config_file.write_text(
        yaml.safe_dump(
            {
                "memory_recall": {
                    "enabled": False,
                    "provider": "local",
                    "model": "llama",
                    "timeout": 12,
                    "candidate_limit": 5,
                    "selected_limit": 2,
                    "min_score": 0.25,
                }
            }
        )
    )
    disabled_config = DaemonConfig(**load_yaml(str(disabled_config_file)))
    assert disabled_config.memory_recall.enabled is False
    assert disabled_config.memory_recall.provider == "local"
    assert disabled_config.memory_recall.model == "llama"
    assert disabled_config.memory_recall.timeout == 12
    assert disabled_config.memory_recall.candidate_limit == 5
    assert disabled_config.memory_recall.selected_limit == 2
    assert disabled_config.memory_recall.min_score == 0.25

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
