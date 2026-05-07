"""Tests for cron configuration."""

from __future__ import annotations

import pytest

from gobby.config.cron import CronConfig

pytestmark = pytest.mark.unit


def test_cron_config_defaults() -> None:
    """CronConfig creates with sensible defaults."""
    config = CronConfig()
    assert config.enabled is True
    assert config.check_interval_seconds == 60
    assert config.max_concurrent_jobs == 5
    assert config.running_timeout_seconds == 600
    assert config.cleanup_after_days == 30
    assert config.backoff_delays == [30, 60, 300, 900, 3600]


def test_cron_config_custom_values() -> None:
    """Custom values override defaults."""
    config = CronConfig(
        enabled=False,
        check_interval_seconds=120,
        max_concurrent_jobs=10,
        running_timeout_seconds=900,
        cleanup_after_days=7,
        backoff_delays=[10, 30, 60],
    )
    assert config.enabled is False
    assert config.check_interval_seconds == 120
    assert config.max_concurrent_jobs == 10
    assert config.running_timeout_seconds == 900
    assert config.cleanup_after_days == 7
    assert config.backoff_delays == [10, 30, 60]


def test_cron_config_clamps_low_check_interval() -> None:
    """check_interval_seconds is normalized to the one-minute scheduler floor."""
    config = CronConfig(check_interval_seconds=30)

    assert config.check_interval_seconds == 60


def test_cron_config_rejects_low_running_timeout() -> None:
    """running_timeout_seconds must be >= 60."""
    with pytest.raises(ValueError, match="at least 60"):
        CronConfig(running_timeout_seconds=59)


def test_cron_config_rejects_zero_max_concurrent() -> None:
    """max_concurrent_jobs must be >= 1."""
    with pytest.raises(ValueError, match="at least 1"):
        CronConfig(max_concurrent_jobs=0)


def test_daemon_config_has_cron_field() -> None:
    """DaemonConfig includes cron field with CronConfig defaults."""
    from gobby.config.app import DaemonConfig

    config = DaemonConfig()
    assert hasattr(config, "cron")
    assert isinstance(config.cron, CronConfig)
    assert config.cron.enabled is True
