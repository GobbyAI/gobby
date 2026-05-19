"""Storage manager for communications."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.id import generate_prefixed_id

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class LocalCommunicationsStore:
    """Storage manager for communications data."""

    def __init__(self, db: HubDatabase, project_id: str = ""):
        """Initialize with database connection and optional project ID."""
        self.db = db
        self.project_id = project_id

    # --- Channels ---

    def create_channel(self, channel: ChannelConfig) -> ChannelConfig:
        """Save a new channel to the database."""
        if not channel.id:
            channel.id = generate_prefixed_id("cc")

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_channels (id, channel_type, name, enabled, config_json, webhook_secret, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel.id,
                    channel.channel_type,
                    channel.name,
                    1 if channel.enabled else 0,
                    json.dumps(channel.config_json),
                    channel.webhook_secret,
                    channel.created_at,
                    channel.updated_at,
                ),
            )
        return channel

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        """Get a channel by ID."""
        row = self.db.fetchone("SELECT * FROM comms_channels WHERE id = ?", (channel_id,))
        return ChannelConfig.from_row(dict(row)) if row else None

    def get_channel_by_name(self, name: str) -> ChannelConfig | None:
        """Get a channel by name."""
        row = self.db.fetchone("SELECT * FROM comms_channels WHERE name = ?", (name,))
        return ChannelConfig.from_row(dict(row)) if row else None

    def list_channels(self, enabled_only: bool = True) -> list[ChannelConfig]:
        """List all channels."""
        sql = "SELECT * FROM comms_channels"
        params: list[Any] = []
        if enabled_only:
            sql += " WHERE enabled = 1"

        rows = self.db.fetchall(sql, tuple(params))
        return [ChannelConfig.from_row(dict(row)) for row in rows]

    def update_channel(self, channel: ChannelConfig) -> ChannelConfig:
        """Update an existing channel."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_channels SET
                    channel_type = ?,
                    name = ?,
                    enabled = ?,
                    config_json = ?,
                    webhook_secret = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    channel.channel_type,
                    channel.name,
                    1 if channel.enabled else 0,
                    json.dumps(channel.config_json),
                    channel.webhook_secret,
                    channel.updated_at,
                    channel.id,
                ),
            )
        return channel

    def delete_channel(self, channel_id: str) -> None:
        """Delete a channel and all related records in a single transaction.

        Cascades to: comms_attachments, comms_messages, comms_identities,
        comms_routing_rules.
        """
        with self.db.transaction() as conn:
            # Delete attachments for channel's messages via subquery
            conn.execute(
                "DELETE FROM comms_attachments WHERE message_id IN "
                "(SELECT id FROM comms_messages WHERE channel_id = ?)",
                (channel_id,),
            )

            # Delete child records
            conn.execute("DELETE FROM comms_messages WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM comms_identities WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM comms_routing_rules WHERE channel_id = ?", (channel_id,))

            # Delete the channel
            conn.execute("DELETE FROM comms_channels WHERE id = ?", (channel_id,))

    # --- Identities ---

    def create_identity(self, identity: CommsIdentity) -> CommsIdentity:
        """Save a new identity to the database."""
        if not identity.id:
            identity.id = generate_prefixed_id("ci")

        if identity.project_id is None and self.project_id:
            identity.project_id = self.project_id

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_identities (
                    id, channel_id, external_user_id, external_username,
                    session_id, project_id, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.id,
                    identity.channel_id,
                    identity.external_user_id,
                    identity.external_username,
                    identity.session_id,
                    identity.project_id,
                    json.dumps(identity.metadata_json),
                    identity.created_at,
                    identity.updated_at,
                ),
            )
        return identity

    def get_identity(self, identity_id: str) -> CommsIdentity | None:
        """Get an identity by ID."""
        row = self.db.fetchone("SELECT * FROM comms_identities WHERE id = ?", (identity_id,))
        return CommsIdentity.from_row(dict(row)) if row else None

    def get_identity_by_external(
        self, channel_id: str, external_user_id: str
    ) -> CommsIdentity | None:
        """Get an identity by channel and external user ID."""
        row = self.db.fetchone(
            "SELECT * FROM comms_identities WHERE channel_id = ? AND external_user_id = ?",
            (channel_id, external_user_id),
        )
        return CommsIdentity.from_row(dict(row)) if row else None

    def list_identities(self, channel_id: str | None = None) -> list[CommsIdentity]:
        """List identities, optionally filtered by channel."""
        sql = "SELECT * FROM comms_identities"
        params: list[Any] = []
        if channel_id:
            sql += " WHERE channel_id = ?"
            params.append(channel_id)

        rows = self.db.fetchall(sql, tuple(params))
        return [CommsIdentity.from_row(dict(row)) for row in rows]

    def find_identities_by_username(self, username: str) -> list[CommsIdentity]:
        """Find identities across all channels by external username."""
        sql = "SELECT * FROM comms_identities WHERE external_username = ?"
        rows = self.db.fetchall(sql, (username,))
        return [CommsIdentity.from_row(dict(row)) for row in rows]

    def update_identity_session(self, identity_id: str, session_id: str | None) -> None:
        """Link or unlink an identity to a session."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE comms_identities SET session_id = ? WHERE id = ?",
                (session_id, identity_id),
            )

    def update_identity(self, identity: CommsIdentity) -> CommsIdentity:
        """Update an existing identity."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_identities SET
                    channel_id = ?,
                    external_user_id = ?,
                    external_username = ?,
                    session_id = ?,
                    project_id = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    identity.channel_id,
                    identity.external_user_id,
                    identity.external_username,
                    identity.session_id,
                    identity.project_id,
                    json.dumps(identity.metadata_json),
                    identity.updated_at,
                    identity.id,
                ),
            )
        return identity

    def delete_identity(self, identity_id: str) -> None:
        """Delete an identity by ID."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM comms_identities WHERE id = ?", (identity_id,))

    # --- Messages ---

    def create_message(self, message: CommsMessage) -> CommsMessage:
        """Save a new message to the database."""
        if not message.id:
            message.id = generate_prefixed_id("cm")

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_messages (
                    id, channel_id, identity_id, direction, content, content_type,
                    platform_message_id, platform_thread_id, session_id, status,
                    error, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.channel_id,
                    message.identity_id,
                    message.direction,
                    message.content,
                    message.content_type,
                    message.platform_message_id,
                    message.platform_thread_id,
                    message.session_id,
                    message.status,
                    message.error,
                    json.dumps(message.metadata_json),
                    message.created_at,
                ),
            )
        return message

    def get_message(self, message_id: str) -> CommsMessage | None:
        """Get a message by ID."""
        row = self.db.fetchone("SELECT * FROM comms_messages WHERE id = ?", (message_id,))
        return CommsMessage.from_row(dict(row)) if row else None

    def get_message_by_platform_id(
        self, channel_name: str, platform_message_id: str
    ) -> CommsMessage | None:
        """Get a message by its platform ID on a specific channel."""
        sql = """
            SELECT m.* FROM comms_messages m
            JOIN comms_channels c ON m.channel_id = c.id
            WHERE c.name = ? AND m.platform_message_id = ?
        """
        row = self.db.fetchone(sql, (channel_name, platform_message_id))
        return CommsMessage.from_row(dict(row)) if row else None

    def list_messages(
        self,
        channel_id: str | None = None,
        session_id: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CommsMessage]:
        """List messages with filters, ordered by created_at DESC."""
        sql = "SELECT * FROM comms_messages WHERE 1=1"
        params: list[Any] = []

        if channel_id:
            sql += " AND channel_id = ?"
            params.append(channel_id)
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if direction:
            sql += " AND direction = ?"
            params.append(direction)

        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(sql, tuple(params))
        return [CommsMessage.from_row(dict(row)) for row in rows]

    def delete_messages_before(self, cutoff: datetime) -> int:
        """Delete messages created before the given cutoff date."""
        cutoff_iso = cutoff.isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM comms_messages WHERE created_at < ?", (cutoff_iso,))
            return cursor.rowcount

    def update_message_status(self, message_id: str, status: str, error: str | None = None) -> None:
        """Update a message's status."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE comms_messages SET status = ?, error = ? WHERE id = ?",
                (status, error, message_id),
            )

    # --- Routing Rules ---

    def create_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Save a new routing rule to the database."""
        if not rule.id:
            rule.id = generate_prefixed_id("cr")

        if rule.project_id is None and self.project_id:
            rule.project_id = self.project_id

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_routing_rules (
                    id, name, channel_id, event_pattern, project_id, session_id,
                    priority, enabled, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.id,
                    rule.name,
                    rule.channel_id,
                    rule.event_pattern,
                    rule.project_id,
                    rule.session_id,
                    rule.priority,
                    1 if rule.enabled else 0,
                    json.dumps(rule.config_json),
                    rule.created_at,
                    rule.updated_at,
                ),
            )
        return rule

    def get_routing_rule(self, rule_id: str) -> CommsRoutingRule | None:
        """Get a routing rule by ID."""
        row = self.db.fetchone("SELECT * FROM comms_routing_rules WHERE id = ?", (rule_id,))
        return CommsRoutingRule.from_row(dict(row)) if row else None

    def list_routing_rules(
        self, channel_id: str | None = None, enabled_only: bool = True
    ) -> list[CommsRoutingRule]:
        """List routing rules."""
        sql = "SELECT * FROM comms_routing_rules WHERE 1=1"
        params: list[Any] = []

        if enabled_only:
            sql += " AND enabled = 1"
        if channel_id:
            sql += " AND (channel_id = ? OR channel_id IS NULL)"
            params.append(channel_id)

        sql += " ORDER BY priority DESC"

        rows = self.db.fetchall(sql, tuple(params))
        return [CommsRoutingRule.from_row(dict(row)) for row in rows]

    def update_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Update an existing routing rule."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_routing_rules SET
                    name = ?,
                    channel_id = ?,
                    event_pattern = ?,
                    project_id = ?,
                    session_id = ?,
                    priority = ?,
                    enabled = ?,
                    config_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    rule.name,
                    rule.channel_id,
                    rule.event_pattern,
                    rule.project_id,
                    rule.session_id,
                    rule.priority,
                    1 if rule.enabled else 0,
                    json.dumps(rule.config_json),
                    rule.updated_at,
                    rule.id,
                ),
            )
        return rule

    def delete_routing_rule(self, rule_id: str) -> None:
        """Delete a routing rule by ID."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM comms_routing_rules WHERE id = ?", (rule_id,))

    # --- Attachments ---

    def create_attachment(self, attachment: CommsAttachment) -> CommsAttachment:
        """Save a new attachment to the database."""
        if not attachment.id:
            attachment.id = generate_prefixed_id("ca")

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_attachments (
                    id, message_id, filename, content_type, size_bytes,
                    local_path, platform_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment.id,
                    attachment.message_id,
                    attachment.filename,
                    attachment.content_type,
                    attachment.size_bytes,
                    attachment.local_path,
                    attachment.platform_url,
                    attachment.created_at,
                ),
            )
        return attachment

    def get_attachment(self, attachment_id: str) -> CommsAttachment | None:
        """Get an attachment by ID."""
        row = self.db.fetchone("SELECT * FROM comms_attachments WHERE id = ?", (attachment_id,))
        return CommsAttachment.from_row(dict(row)) if row else None

    def list_attachments(self, message_id: str) -> list[CommsAttachment]:
        """List all attachments for a message."""
        rows = self.db.fetchall(
            "SELECT * FROM comms_attachments WHERE message_id = ? ORDER BY created_at",
            (message_id,),
        )
        return [CommsAttachment.from_row(dict(row)) for row in rows]

    def delete_attachment(self, attachment_id: str) -> None:
        """Delete an attachment by ID."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM comms_attachments WHERE id = ?", (attachment_id,))

    def delete_attachments_for_message(self, message_id: str) -> int:
        """Delete all attachments for a message."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM comms_attachments WHERE message_id = ?", (message_id,)
            )
            return cursor.rowcount
