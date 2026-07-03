"""Communications data models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from gobby.utils.datetime import normalize_datetime_model, utc_now


def _required(data: Mapping[str, Any], key: str) -> Any:
    value = data[key]
    if value is None:
        raise ValueError(f"Required communication row field {key!r} is null")
    return value


@dataclass
class ChannelCapabilities:
    """Capabilities of a communication channel."""

    threading: bool = False
    reactions: bool = False
    files: bool = False
    markdown: bool = False
    max_message_length: int = 4000

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelCapabilities:
        """Create from dictionary."""
        return cls(
            threading=data.get("threading", False),
            reactions=data.get("reactions", False),
            files=data.get("files", False),
            markdown=data.get("markdown", False),
            max_message_length=data.get("max_message_length", 4000),
        )

    @classmethod
    def from_row(cls, row: Any) -> ChannelCapabilities:
        """Create from database row."""
        data = dict(row)
        return cls(
            threading=bool(data.get("threading", False)),
            reactions=bool(data.get("reactions", False)),
            files=bool(data.get("files", False)),
            markdown=bool(data.get("markdown", False)),
            max_message_length=data.get("max_message_length", 4000),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "threading": self.threading,
            "reactions": self.reactions,
            "files": self.files,
            "markdown": self.markdown,
            "max_message_length": self.max_message_length,
        }


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    )
)
@dataclass
class ChannelConfig:
    """Configuration for a communication channel."""

    id: str
    channel_type: str
    name: str
    enabled: bool
    config_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    webhook_secret: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ChannelConfig:
        """Create from database row."""
        data = dict(row)
        config_json = data.get("config_json")
        if isinstance(config_json, str):
            import json

            config_json = json.loads(config_json)

        return cls(
            id=_required(data, "id"),
            channel_type=_required(data, "channel_type"),
            name=_required(data, "name"),
            enabled=bool(data.get("enabled", False)),
            config_json=config_json or {},
            webhook_secret=data.get("webhook_secret"),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )


@normalize_datetime_model(required=("created_at",))
@dataclass
class CommsMessage:
    """A message sent or received via a communication channel.

    ``channel_id`` is always the internal ``comms_channels.id`` value. It is
    never a Slack channel, SMS number, Teams conversation, Discord channel,
    Telegram chat, email sender, or Gobby Chat destination. Outbound adapters
    read their platform destination from ``metadata_json["platform_destination"]``;
    inbound adapters preserve their platform channel in
    ``metadata_json["platform_channel_id"]`` before storage.

    Inbound adapters must use ``identity_id`` for the platform-stable user id
    and must store the platform username/display name in
    ``metadata_json["external_username"]``. When a platform has no distinct
    username field, adapters should use the same stable platform id for
    ``external_username``.
    """

    id: str
    channel_id: str
    direction: Literal["inbound", "outbound"]
    content: str
    created_at: datetime
    identity_id: str | None = None
    content_type: str = "text"
    platform_message_id: str | None = None
    platform_thread_id: str | None = None
    session_id: str | None = None
    status: str = "sent"
    error: str | None = None
    metadata_json: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CommsMessage:
        """Create from database row."""
        data = dict(row)
        metadata_json = data.get("metadata_json")
        if isinstance(metadata_json, str):
            import json

            metadata_json = json.loads(metadata_json)

        return cls(
            id=_required(data, "id"),
            channel_id=_required(data, "channel_id"),
            identity_id=data.get("identity_id"),
            direction=_required(data, "direction"),
            content=_required(data, "content"),
            content_type=data.get("content_type", "text"),
            platform_message_id=data.get("platform_message_id"),
            platform_thread_id=data.get("platform_thread_id"),
            session_id=data.get("session_id"),
            status=data.get("status", "sent"),
            error=data.get("error"),
            metadata_json=metadata_json or {},
            created_at=data.get("created_at", utc_now()),
        )


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    )
)
@dataclass
class CommsIdentity:
    """An identity (user) on a communication channel."""

    id: str
    channel_id: str
    external_user_id: str
    created_at: datetime
    updated_at: datetime
    external_username: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    metadata_json: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CommsIdentity:
        """Create from database row."""
        data = dict(row)
        metadata_json = data.get("metadata_json")
        if isinstance(metadata_json, str):
            import json

            metadata_json = json.loads(metadata_json)

        return cls(
            id=_required(data, "id"),
            channel_id=_required(data, "channel_id"),
            external_user_id=_required(data, "external_user_id"),
            external_username=data.get("external_username"),
            session_id=data.get("session_id"),
            project_id=data.get("project_id"),
            metadata_json=metadata_json or {},
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )


@normalize_datetime_model(required=("created_at",))
@dataclass
class CommsAttachment:
    """A file attachment on a communication message."""

    id: str
    message_id: str
    filename: str
    content_type: str
    size_bytes: int
    local_path: str | None = None
    platform_url: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CommsAttachment:
        """Create from database row."""
        data = dict(row)
        return cls(
            id=_required(data, "id"),
            message_id=_required(data, "message_id"),
            filename=_required(data, "filename"),
            content_type=data.get("content_type", "application/octet-stream"),
            size_bytes=int(data.get("size_bytes", 0)),
            local_path=data.get("local_path"),
            platform_url=data.get("platform_url"),
            created_at=data.get("created_at", utc_now()),
        )


@normalize_datetime_model(
    optional=(
        "created_at",
        "updated_at",
    )
)
@dataclass
class CommsRoutingRule:
    """A rule for routing inbound communication events."""

    id: str
    name: str
    channel_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    event_pattern: str = "*"
    project_id: str | None = None
    session_id: str | None = None
    priority: int = 0
    enabled: bool = True
    config_json: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CommsRoutingRule:
        """Create from database row."""
        data = dict(row)
        config_json = data.get("config_json")
        if isinstance(config_json, str):
            import json

            config_json = json.loads(config_json)

        return cls(
            id=_required(data, "id"),
            name=_required(data, "name"),
            channel_id=data.get("channel_id"),
            event_pattern=_required(data, "event_pattern"),
            project_id=data.get("project_id"),
            session_id=data.get("session_id"),
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            config_json=config_json or {},
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )
