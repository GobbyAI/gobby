"""Tests for communication models."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.communications.models import (
    ChannelCapabilities,
    ChannelConfig,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)


def test_channel_capabilities_from_row() -> None:
    row = {
        "threading": 1,
        "reactions": 0,
        "files": 1,
        "markdown": 1,
        "max_message_length": 4096,
    }
    caps = ChannelCapabilities.from_row(row)
    assert caps.threading is True
    assert caps.reactions is False
    assert caps.files is True
    assert caps.markdown is True
    assert caps.max_message_length == 4096
    assert caps.to_dict() == {
        "threading": True,
        "reactions": False,
        "files": True,
        "markdown": True,
        "max_message_length": 4096,
    }


def test_channel_config_from_row() -> None:
    row = {
        "id": "slack-1",
        "channel_type": "slack",
        "name": "General Slack",
        "enabled": 1,
        "config_json": json.dumps({"token": "xoxb-123"}),
        "webhook_secret": "secret-123",
        "created_at": "2026-03-21T00:00:00Z",
        "updated_at": "2026-03-21T00:00:00Z",
    }
    config = ChannelConfig.from_row(row)
    assert config.id == "slack-1"
    assert config.channel_type == "slack"
    assert config.name == "General Slack"
    assert config.enabled is True
    assert config.config_json == {"token": "xoxb-123"}
    assert config.webhook_secret == "secret-123"
    expected_timestamp = datetime(2026, 3, 21, tzinfo=UTC)
    assert config.created_at == expected_timestamp
    assert config.updated_at == expected_timestamp


def test_comms_message_from_row() -> None:
    row = {
        "id": "msg-1",
        "channel_id": "slack-1",
        "identity_id": "user-1",
        "direction": "inbound",
        "content": "Hello world",
        "content_type": "text",
        "platform_message_id": "ts-123",
        "platform_thread_id": "thread-456",
        "session_id": "sess-789",
        "status": "received",
        "error": None,
        "metadata_json": json.dumps({"foo": "bar"}),
        "created_at": "2026-03-21T12:00:00Z",
    }
    msg = CommsMessage.from_row(row)
    assert msg.id == "msg-1"
    assert msg.channel_id == "slack-1"
    assert msg.identity_id == "user-1"
    assert msg.direction == "inbound"
    assert msg.content == "Hello world"
    assert msg.content_type == "text"
    assert msg.platform_message_id == "ts-123"
    assert msg.platform_thread_id == "thread-456"
    assert msg.session_id == "sess-789"
    assert msg.status == "received"
    assert msg.error is None
    assert msg.metadata_json == {"foo": "bar"}
    assert msg.created_at == datetime(2026, 3, 21, 12, tzinfo=UTC)


def test_comms_identity_from_row() -> None:
    row = {
        "id": "ident-1",
        "channel_id": "slack-1",
        "external_user_id": "U123",
        "external_username": "jdoe",
        "session_id": "sess-789",
        "project_id": "proj-1",
        "metadata_json": json.dumps({"team_id": "T456"}),
        "created_at": "2026-03-21T00:00:00Z",
        "updated_at": "2026-03-21T00:00:00Z",
    }
    ident = CommsIdentity.from_row(row)
    assert ident.id == "ident-1"
    assert ident.channel_id == "slack-1"
    assert ident.external_user_id == "U123"
    assert ident.external_username == "jdoe"
    assert ident.session_id == "sess-789"
    assert ident.project_id == "proj-1"
    assert ident.metadata_json == {"team_id": "T456"}


def test_comms_routing_rule_from_row() -> None:
    row = {
        "id": "rule-1",
        "name": "Slack to Session",
        "channel_id": "slack-1",
        "event_pattern": "message.*",
        "project_id": "proj-1",
        "session_id": "sess-789",
        "priority": 10,
        "enabled": 1,
        "config_json": json.dumps({"auto_reply": True}),
        "created_at": "2026-03-21T00:00:00Z",
        "updated_at": "2026-03-21T00:00:00Z",
    }
    rule = CommsRoutingRule.from_row(row)
    assert rule.id == "rule-1"
    assert rule.name == "Slack to Session"
    assert rule.channel_id == "slack-1"
    assert rule.event_pattern == "message.*"
    assert rule.project_id == "proj-1"
    assert rule.session_id == "sess-789"
    assert rule.priority == 10
    assert rule.enabled is True
    assert rule.config_json == {"auto_reply": True}


def test_models_with_missing_fields_from_row() -> None:
    # Only required fields provided in the row
    row = {
        "id": "rule-min",
        "name": "Minimal Rule",
        "event_pattern": "*",
        "project_id": "proj-1",
        "created_at": "2026-03-21T00:00:00Z",
        "updated_at": "2026-03-21T00:00:00Z",
    }
    # For CommsRoutingRule, config_json is required in __init__ but we've refactored
    # to use _parse_json_field which handles missing keys.
    # Wait, in my refactored CommsRoutingRule, config_json has a default_factory=dict.
    rule = CommsRoutingRule.from_row(row)
    assert rule.id == "rule-min"
    assert rule.name == "Minimal Rule"
    assert rule.channel_id is None
    assert rule.event_pattern == "*"
    assert rule.priority == 0
    assert rule.enabled is True
    assert rule.config_json == {}

    # Test CommsMessage with missing fields
    msg_row = {
        "id": "msg-min",
        "channel_id": "chan-1",
        "direction": "outbound",
        "content": "Minimal content",
    }
    msg = CommsMessage.from_row(msg_row)
    assert msg.id == "msg-min"
    assert msg.content_type == "text"
    assert msg.status == "sent"
    assert msg.metadata_json == {}
    assert msg.identity_id is None


def test_from_row_rejects_missing_required_fields() -> None:
    with pytest.raises(KeyError):
        ChannelConfig.from_row({"id": "chan-1", "name": "General"})

    with pytest.raises(KeyError):
        CommsMessage.from_row({"id": "msg-1", "content": "hello", "direction": "outbound"})

    with pytest.raises(KeyError):
        CommsIdentity.from_row({"id": "ident-1", "channel_id": "chan-1"})

    with pytest.raises(KeyError):
        CommsRoutingRule.from_row({"id": "rule-1", "name": "Rule"})
