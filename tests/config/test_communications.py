"""Tests for communication configuration models."""

import pytest
from pydantic import ValidationError

from gobby.config.app import DaemonConfig
from gobby.config.communications import ChannelDefaults, CommunicationsConfig


def test_channel_defaults_pydantic():
    """Test ChannelDefaults with custom values."""
    defaults = ChannelDefaults(
        rate_limit_per_minute=60,
        burst=10,
        retry_count=5,
        poll_interval_seconds=15,
        retention_days=30,
    )
    assert defaults.rate_limit_per_minute == 60
    assert defaults.burst == 10
    assert defaults.retry_count == 5
    assert defaults.poll_interval_seconds == 15
    assert defaults.retention_days == 30


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rate_limit_per_minute", 0),
        ("rate_limit_per_minute", -1),
        ("burst", 0),
        ("burst", -1),
    ],
)
def test_channel_defaults_reject_non_positive_pacing(field: str, value: int):
    """ChannelDefaults rejects unsafe pacing values."""
    with pytest.raises(ValidationError):
        ChannelDefaults(**{field: value})


def test_communications_config_pydantic():
    """Test CommunicationsConfig with custom values."""
    config = CommunicationsConfig(
        enabled=True,
        webhook_base_url="https://api.gobby.ai/webhooks",
        auto_create_sessions=False,
    )
    assert config.enabled is True
    assert config.webhook_base_url == "https://api.gobby.ai/webhooks"
    assert config.auto_create_sessions is False
    assert config.channel_defaults.rate_limit_per_minute == 30  # Default


def test_communications_config_default():
    """Test CommunicationsConfig defaults."""
    config = CommunicationsConfig()
    assert config.enabled is False
    assert config.webhook_base_url == ""
    assert config.auto_create_sessions is True
    assert isinstance(config.channel_defaults, ChannelDefaults)
    assert config.channel_defaults.rate_limit_per_minute == 30


def test_daemon_config_includes_communications():
    """Test CommunicationsConfig is wired into DaemonConfig."""
    config = DaemonConfig()
    assert hasattr(config, "communications")
    assert isinstance(config.communications, CommunicationsConfig)
    assert config.communications.enabled is False
