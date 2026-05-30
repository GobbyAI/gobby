"""Tests for session-related configuration models."""

from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig, load_yaml
from gobby.config.sessions import MemoryRecallHelperConfig

pytestmark = pytest.mark.unit


def test_memory_recall_helper_config_shape(temp_dir: Path) -> None:
    """Memory recall helper config exposes daemon-owned runner controls."""
    assert set(MemoryRecallHelperConfig.model_fields) == {
        "provider",
        "model",
        "tier",
        "enabled",
        "timeout",
        "candidate_limit",
        "selected_limit",
        "min_score",
    }
    cfg = MemoryRecallHelperConfig()
    assert cfg.enabled is True
    assert cfg.provider == "claude"
    assert cfg.model == "haiku"
    assert cfg.tier == "low"
    assert cfg.timeout == 60
    assert cfg.candidate_limit == 8
    assert cfg.selected_limit == 3
    assert cfg.min_score == 0.5
    assert DaemonConfig().memory_recall_helper.enabled is True

    disabled_config_file = temp_dir / "disabled.yaml"
    disabled_config_file.write_text(
        yaml.safe_dump(
            {
                "memory_recall_helper": {
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
    assert disabled_config.memory_recall_helper.enabled is False
    assert disabled_config.memory_recall_helper.provider == "local"
    assert disabled_config.memory_recall_helper.model == "llama"
    assert disabled_config.memory_recall_helper.timeout == 12
    assert disabled_config.memory_recall_helper.candidate_limit == 5
    assert disabled_config.memory_recall_helper.selected_limit == 2
    assert disabled_config.memory_recall_helper.min_score == 0.25

    default_config_file = temp_dir / "default.yaml"
    default_config_file.write_text(yaml.safe_dump({"daemon_port": 60999}))
    default_config = DaemonConfig(**load_yaml(str(default_config_file)))
    assert default_config.memory_recall_helper.enabled is True
