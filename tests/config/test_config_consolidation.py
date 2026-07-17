"""Tests for config consolidation and telemetry cleanup."""

import pytest

from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


def test_logging_config_is_first_class() -> None:
    cfg = DaemonConfig(logging={"level": "debug"})
    assert cfg.logging.level == "debug"
