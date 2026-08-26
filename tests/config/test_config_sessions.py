"""Tests for session-related configuration models."""

from pathlib import Path

import pytest
import yaml

from gobby.config.app import DaemonConfig, load_yaml

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("removed_key", ["memory_recall_helper", "memory_recall"])
def test_removed_memory_recall_config_fails_loud(temp_dir: Path, removed_key: str) -> None:
    """Stale recall config should be visible at startup."""
    config_file = temp_dir / "stale-recall.yaml"
    config_file.write_text(yaml.safe_dump({removed_key: {"enabled": False}}))

    with pytest.raises(ValueError, match=f"{removed_key} config has been removed"):
        DaemonConfig(**load_yaml(str(config_file)))


def test_daemon_config_validates_without_a_recall_timeout() -> None:
    """1.1.3: the load-order validator lost its first term and must still hold."""
    assert "memory_recall" not in DaemonConfig.model_fields
    config = DaemonConfig()
    assert config.workflow.timeout < config.hooks.adapter_timeout
    assert config.hooks.adapter_timeout < config.hooks.provider_timeout
