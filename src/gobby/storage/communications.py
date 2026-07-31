"""Storage manager for communications."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import (
    parse_stored_datetime,
    to_aware_utc,
    utc_now,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import Transaction

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
            channel.id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_channels (id, channel_type, name, enabled, config_json, webhook_secret, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    channel.id,
                    channel.channel_type,
                    channel.name,
                    bool(channel.enabled),
                    json.dumps(channel.config_json),
                    channel.webhook_secret,
                    channel.created_at,
                    channel.updated_at,
                ),
            )
        return channel

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        """Get a channel by ID."""
        row = self.db.fetchone("SELECT * FROM comms_channels WHERE id = %s", (channel_id,))
        return ChannelConfig.from_row(dict(row)) if row else None

    def get_channel_by_name(self, name: str) -> ChannelConfig | None:
        """Get a channel by name."""
        row = self.db.fetchone("SELECT * FROM comms_channels WHERE name = %s", (name,))
        return ChannelConfig.from_row(dict(row)) if row else None

    def list_channels(self, enabled_only: bool = True) -> list[ChannelConfig]:
        """List all channels."""
        sql = "SELECT * FROM comms_channels"
        params: list[Any] = []
        if enabled_only:
            sql += " WHERE enabled IS TRUE"

        rows = self.db.fetchall(sql, tuple(params))
        return [ChannelConfig.from_row(dict(row)) for row in rows]

    def update_channel(self, channel: ChannelConfig) -> ChannelConfig:
        """Update an existing channel."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_channels SET
                    channel_type = %s,
                    name = %s,
                    enabled = %s,
                    config_json = %s,
                    webhook_secret = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    channel.channel_type,
                    channel.name,
                    bool(channel.enabled),
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
                "(SELECT id FROM comms_messages WHERE channel_id = %s)",
                (channel_id,),
            )

            # Delete child records
            conn.execute("DELETE FROM comms_messages WHERE channel_id = %s", (channel_id,))
            conn.execute("DELETE FROM comms_identities WHERE channel_id = %s", (channel_id,))
            conn.execute("DELETE FROM comms_routing_rules WHERE channel_id = %s", (channel_id,))

            # Delete the channel
            conn.execute("DELETE FROM comms_channels WHERE id = %s", (channel_id,))

    # --- Identities ---

    def create_identity(self, identity: CommsIdentity) -> CommsIdentity:
        """Save a new identity to the database."""
        if not identity.id:
            identity.id = str(uuid.uuid4())

        now = utc_now()
        if not identity.created_at:
            identity.created_at = now
        if not identity.updated_at:
            identity.updated_at = now
        created_at_value = parse_stored_datetime(identity.created_at)
        updated_at_value = parse_stored_datetime(identity.updated_at)

        if identity.project_id is None and self.project_id:
            identity.project_id = self.project_id

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_identities (
                    id, channel_id, external_user_id, external_username,
                    session_id, project_id, metadata_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    identity.id,
                    identity.channel_id,
                    identity.external_user_id,
                    identity.external_username,
                    identity.session_id,
                    identity.project_id,
                    json.dumps(identity.metadata_json),
                    created_at_value,
                    updated_at_value,
                ),
            )
        return identity

    def get_identity(self, identity_id: str) -> CommsIdentity | None:
        """Get an identity by ID."""
        row = self.db.fetchone("SELECT * FROM comms_identities WHERE id = %s", (identity_id,))
        return CommsIdentity.from_row(dict(row)) if row else None

    def get_identity_by_external(
        self, channel_id: str, external_user_id: str
    ) -> CommsIdentity | None:
        """Get an identity by channel and external user ID."""
        row = self.db.fetchone(
            "SELECT * FROM comms_identities WHERE channel_id = %s AND external_user_id = %s",
            (channel_id, external_user_id),
        )
        return CommsIdentity.from_row(dict(row)) if row else None

    def list_identities(self, channel_id: str | None = None) -> list[CommsIdentity]:
        """List identities, optionally filtered by channel."""
        sql = "SELECT * FROM comms_identities"
        params: list[Any] = []
        if channel_id:
            sql += " WHERE channel_id = %s"
            params.append(channel_id)

        rows = self.db.fetchall(sql, tuple(params))
        return [CommsIdentity.from_row(dict(row)) for row in rows]

    def find_identities_by_username(self, username: str) -> list[CommsIdentity]:
        """Find identities across all channels by external username."""
        sql = "SELECT * FROM comms_identities WHERE external_username = %s"
        rows = self.db.fetchall(sql, (username,))
        return [CommsIdentity.from_row(dict(row)) for row in rows]

    def update_identity_session(self, identity_id: str, session_id: str | None) -> None:
        """Link or unlink an identity to a session."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE comms_identities SET session_id = %s WHERE id = %s",
                (session_id, identity_id),
            )

    def update_identity(self, identity: CommsIdentity) -> CommsIdentity:
        """Update an existing identity."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_identities SET
                    channel_id = %s,
                    external_user_id = %s,
                    external_username = %s,
                    session_id = %s,
                    project_id = %s,
                    metadata_json = %s,
                    updated_at = %s
                WHERE id = %s
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
            conn.execute("DELETE FROM comms_identities WHERE id = %s", (identity_id,))

    # --- Messages ---

    def create_message(self, message: CommsMessage) -> CommsMessage:
        """Save a new message to the database."""
        persisted, _ = self.create_message_with_attachments(message, [])
        return persisted

    def create_message_with_attachments(
        self,
        message: CommsMessage,
        attachments: list[CommsAttachment],
    ) -> tuple[CommsMessage, list[CommsAttachment]]:
        """Save a message and its attachments in one transaction."""
        if not message.id:
            message.id = str(uuid.uuid4())
        for attachment in attachments:
            if not attachment.id:
                attachment.id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            row = conn.execute(
                """
                INSERT INTO comms_messages (
                    id, channel_id, identity_id, direction, content, content_type,
                    platform_message_id, platform_thread_id, session_id, status,
                    error, metadata_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (channel_id, platform_message_id)
                WHERE platform_message_id IS NOT NULL
                DO NOTHING
                RETURNING *
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
            ).fetchone()
            inserted = row is not None
            if row is None and message.platform_message_id is not None:
                row = conn.execute(
                    """
                    SELECT *
                      FROM comms_messages
                     WHERE channel_id = %s AND platform_message_id = %s
                    """,
                    (message.channel_id, message.platform_message_id),
                ).fetchone()
            if row is None:
                raise RuntimeError("Failed to create communications message")

            persisted = CommsMessage.from_row(row)
            saved_attachments: list[CommsAttachment] = []
            if inserted:
                for attachment in attachments:
                    attachment.message_id = persisted.id
                    self._insert_attachment(conn, attachment)
                    saved_attachments.append(attachment)
            elif attachments:
                logger.info(
                    "Skipped attachment persistence for deduplicated communications message",
                    extra={
                        "message_id": persisted.id,
                        "platform_message_id": message.platform_message_id,
                    },
                )

        return persisted, saved_attachments

    def get_message(self, message_id: str) -> CommsMessage | None:
        """Get a message by ID."""
        row = self.db.fetchone("SELECT * FROM comms_messages WHERE id = %s", (message_id,))
        return CommsMessage.from_row(dict(row)) if row else None

    def get_message_by_platform_id(
        self, channel_name: str, platform_message_id: str
    ) -> CommsMessage | None:
        """Get a message by its platform ID on a specific channel."""
        sql = """
            SELECT m.* FROM comms_messages m
            JOIN comms_channels c ON m.channel_id = c.id
            WHERE c.name = %s
              AND (
                    m.platform_message_id = %s
                 OR m.metadata_json->'platform_message_ids' ? %s
              )
        """
        row = self.db.fetchone(
            sql,
            (channel_name, platform_message_id, platform_message_id),
        )
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
            sql += " AND channel_id = %s"
            params.append(channel_id)
        if session_id:
            sql += " AND session_id = %s"
            params.append(session_id)
        if direction:
            sql += " AND direction = %s"
            params.append(direction)

        sql += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = self.db.fetchall(sql, tuple(params))
        return [CommsMessage.from_row(dict(row)) for row in rows]

    def delete_messages_before(
        self, cutoff: datetime, *, limit: int = 500
    ) -> tuple[int, list[str]]:
        """Delete old messages and return their count and attachment paths."""
        cutoff_value = to_aware_utc(cutoff)
        with self.db.transaction() as conn:
            row = conn.execute(
                """
WITH messages_to_delete AS MATERIALIZED (
    SELECT id FROM comms_messages
    WHERE created_at < %s
    ORDER BY created_at ASC
    LIMIT %s
), attachment_paths AS MATERIALIZED (
    SELECT local_path
    FROM comms_attachments
    WHERE message_id IN (SELECT id FROM messages_to_delete)
      AND local_path IS NOT NULL
), deleted_messages AS (
    DELETE FROM comms_messages
    WHERE id IN (SELECT id FROM messages_to_delete)
    RETURNING id
)
SELECT
    (SELECT COUNT(*) FROM deleted_messages) AS deleted_count,
    COALESCE(
        (SELECT ARRAY_AGG(local_path) FROM attachment_paths),
        ARRAY[]::TEXT[]
    ) AS local_paths
""",
                (cutoff_value, limit),
            ).fetchone()
            if row is None:
                return 0, []
            local_paths = cast(list[str], json.loads(row["local_paths"]))
            return int(row["deleted_count"]), local_paths

    def update_message_status(self, message_id: str, status: str, error: str | None = None) -> None:
        """Update a message's status."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE comms_messages SET status = %s, error = %s WHERE id = %s",
                (status, error, message_id),
            )

    def update_message_delivery(
        self,
        message_id: str,
        status: str,
        error: str | None,
        platform_message_id: str | None,
        metadata_json: dict[str, Any],
    ) -> None:
        """Persist the final delivery result for a reserved outbound message."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_messages
                   SET status = %s,
                       error = %s,
                       platform_message_id = %s,
                       metadata_json = %s
                 WHERE id = %s
                """,
                (
                    status,
                    error,
                    platform_message_id,
                    json.dumps(metadata_json),
                    message_id,
                ),
            )

    def update_message_content(self, message_id: str, content: str) -> None:
        """Replace persisted content after a successful platform edit."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE comms_messages SET content = %s WHERE id = %s",
                (content, message_id),
            )

    # --- Routing Rules ---

    def create_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Save a new routing rule to the database."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO comms_routing_rules (
                    id, name, channel_id, event_pattern, project_id, session_id,
                    priority, enabled, config_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    rule.id,
                    rule.name,
                    rule.channel_id,
                    rule.event_pattern,
                    rule.project_id,
                    rule.session_id,
                    rule.priority,
                    bool(rule.enabled),
                    json.dumps(rule.config_json),
                    rule.created_at,
                    rule.updated_at,
                ),
            )
        return rule

    def get_routing_rule(self, rule_id: str) -> CommsRoutingRule | None:
        """Get a routing rule by ID."""
        row = self.db.fetchone("SELECT * FROM comms_routing_rules WHERE id = %s", (rule_id,))
        return CommsRoutingRule.from_row(dict(row)) if row else None

    def list_routing_rules(
        self,
        channel_id: str | None = None,
        project_id: str | None = None,
        global_scope: bool | None = None,
        enabled: bool | None = None,
        event_pattern: str | None = None,
    ) -> list[CommsRoutingRule]:
        """List routing rules using exact administrative filters."""
        sql = "SELECT * FROM comms_routing_rules WHERE 1=1"
        params: list[Any] = []

        if channel_id is not None:
            sql += " AND channel_id = %s"
            params.append(channel_id)
        if global_scope is True:
            sql += " AND project_id IS NULL"
        elif project_id is not None:
            sql += " AND project_id = %s"
            params.append(project_id)
        elif global_scope is False:
            sql += " AND project_id IS NOT NULL"
        if enabled is not None:
            sql += " AND enabled = %s"
            params.append(enabled)
        if event_pattern is not None:
            sql += " AND event_pattern = %s"
            params.append(event_pattern)

        sql += " ORDER BY priority DESC, created_at ASC, id ASC"

        rows = self.db.fetchall(sql, tuple(params))
        return [CommsRoutingRule.from_row(dict(row)) for row in rows]

    def update_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Update an existing routing rule."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_routing_rules SET
                    name = %s,
                    channel_id = %s,
                    event_pattern = %s,
                    project_id = %s,
                    session_id = %s,
                    priority = %s,
                    enabled = %s,
                    config_json = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    rule.name,
                    rule.channel_id,
                    rule.event_pattern,
                    rule.project_id,
                    rule.session_id,
                    rule.priority,
                    bool(rule.enabled),
                    json.dumps(rule.config_json),
                    rule.updated_at,
                    rule.id,
                ),
            )
        return rule

    def delete_routing_rule(self, rule_id: str) -> None:
        """Delete a routing rule by ID."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM comms_routing_rules WHERE id = %s", (rule_id,))

    # --- Attachments ---

    @staticmethod
    def _insert_attachment(conn: Transaction, attachment: CommsAttachment) -> None:
        conn.execute(
            """
            INSERT INTO comms_attachments (
                id, message_id, filename, content_type, size_bytes,
                local_path, platform_url, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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

    def create_attachment(self, attachment: CommsAttachment) -> CommsAttachment:
        """Save a new attachment to the database."""
        if not attachment.id:
            attachment.id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            self._insert_attachment(conn, attachment)
        return attachment

    def get_attachment(self, attachment_id: str) -> CommsAttachment | None:
        """Get an attachment by ID."""
        row = self.db.fetchone("SELECT * FROM comms_attachments WHERE id = %s", (attachment_id,))
        return CommsAttachment.from_row(dict(row)) if row else None

    def list_attachments(self, message_id: str) -> list[CommsAttachment]:
        """List all attachments for a message."""
        rows = self.db.fetchall(
            "SELECT * FROM comms_attachments WHERE message_id = %s ORDER BY created_at",
            (message_id,),
        )
        return [CommsAttachment.from_row(dict(row)) for row in rows]

    def delete_attachment(self, attachment_id: str) -> None:
        """Delete an attachment by ID."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM comms_attachments WHERE id = %s", (attachment_id,))

    def delete_attachments_for_message(self, message_id: str) -> int:
        """Delete all attachments for a message."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM comms_attachments WHERE message_id = %s", (message_id,)
            )
            return cursor.rowcount
