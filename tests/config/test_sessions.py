"""Tests for session-related configuration models."""

from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig, load_yaml
from gobby.config.sessions import MemoryRecallHelperConfig

pytestmark = pytest.mark.unit


def test_memory_recall_helper_config_shape(temp_dir: Path) -> None:
    """Memory recall helper config exposes only its runtime toggle."""
    assert set(MemoryRecallHelperConfig.model_fields) == {"enabled"}
    assert MemoryRecallHelperConfig().enabled is True
    assert DaemonConfig().memory_recall_helper.enabled is True

    disabled_config_file = temp_dir / "disabled.yaml"
    disabled_config_file.write_text(yaml.safe_dump({"memory_recall_helper": {"enabled": False}}))
    disabled_config = DaemonConfig(**load_yaml(str(disabled_config_file)))
    assert disabled_config.memory_recall_helper.enabled is False

    default_config_file = temp_dir / "default.yaml"
    default_config_file.write_text(yaml.safe_dump({"daemon_port": 60999}))
    default_config = DaemonConfig(**load_yaml(str(default_config_file)))
    assert default_config.memory_recall_helper.enabled is True
