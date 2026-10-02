"""Storage manager for communications."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import ensure_system_session, system_session_id
from gobby.utils.datetime import to_aware_utc, utc_now
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import Transaction

logger = logging.getLogger(__name__)


class LocalCommunicationsStore:
    """Storage manager for communications data."""

    def __init__(
        self,
        db: HubDatabase,
        project_id: str = "",
        *,
        machine_id: str | None = None,
    ):
        """Initialize with database connection and optional project ID."""
        self.db = db
        self.project_id = project_id
        self.machine_id = machine_id or require_machine_id()

    # --- Channels ---

    def create_channel(self, channel: ChannelConfig) -> ChannelConfig:
        """Save a new channel to the database."""
        if not channel.id:
            channel.id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            row = conn.execute(
                """
                INSERT INTO comms_channels (id, channel_type, name, enabled, config_json, webhook_secret)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING created_at, updated_at
                """,
                (
                    channel.id,
                    channel.channel_type,
                    channel.name,
                    bool(channel.enabled),
                    json.dumps(channel.config_json),
                    channel.webhook_secret,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create communications channel")
        channel.created_at = row["created_at"]
        channel.updated_at = row["updated_at"]
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

    def set_telegram_agent_target(
        self, channel_id: str, conversation_id: str, session_id: str | None
    ) -> None:
        """Atomically update one private-chat target in the channel's durable config."""
        with self.db.transaction() as conn:
            if session_id is None:
                conn.execute(
                    """
                    UPDATE comms_channels
                    SET config_json = jsonb_set(
                        config_json, '{telegram_agent_targets}',
                        COALESCE(config_json->'telegram_agent_targets', '{}'::jsonb) - %s,
                        true
                    ), updated_at = now()
                    WHERE id = %s
                    """,
                    (conversation_id, channel_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE comms_channels
                    SET config_json = jsonb_set(
                        config_json, '{telegram_agent_targets}',
                        COALESCE(config_json->'telegram_agent_targets', '{}'::jsonb)
                            || jsonb_build_object(%s::text, %s::text),
                        true
                    ), updated_at = now()
                    WHERE id = %s
                    """,
                    (conversation_id, session_id, channel_id),
                )

    def merge_channel_config(
        self, channel_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Merge adapter settings without overwriting concurrent target selections."""
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE comms_channels
                SET config_json = config_json || %s::jsonb, updated_at = now()
                WHERE id = %s
                RETURNING config_json
                """,
                (json.dumps(values), channel_id),
            ).fetchone()
        if row is None:
            return None
        config = row["config_json"]
        return json.loads(config) if isinstance(config, str) else dict(config)

    def delete_channel(self, channel_id: str) -> None:
        """Delete a channel and all related records in a single transaction.

        Cascades to: comms_attachments, comms_messages, comms_identities.
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

            # Delete the channel
            conn.execute("DELETE FROM comms_channels WHERE id = %s", (channel_id,))

    # --- Identities ---

    def create_identity(self, identity: CommsIdentity) -> CommsIdentity:
        """Save a new identity to the database."""
        if not identity.id:
            identity.id = str(uuid.uuid4())

        if identity.project_id is None and self.project_id:
            identity.project_id = self.project_id

        with self.db.transaction() as conn:
            row = conn.execute(
                """
                INSERT INTO comms_identities (
                    id, channel_id, external_user_id, external_username,
                    session_id, project_id, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING created_at, updated_at
                """,
                (
                    identity.id,
                    identity.channel_id,
                    identity.external_user_id,
                    identity.external_username,
                    identity.session_id,
                    identity.project_id,
                    json.dumps(identity.metadata_json),
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create communications identity")
        identity.created_at = row["created_at"]
        identity.updated_at = row["updated_at"]
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
                """
                UPDATE comms_identities
                SET session_id = %s, updated_at = %s
                WHERE id = %s AND session_id IS DISTINCT FROM %s
                """,
                (session_id, utc_now(), identity_id, session_id),
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
        for attachment in attachments:
            if not attachment.id:
                attachment.id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            persisted, inserted = self._insert_message(conn, message)
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

    def _insert_message(
        self, conn: Transaction, message: CommsMessage
    ) -> tuple[CommsMessage, bool]:
        """Insert a message, or return the existing platform duplicate; True when inserted."""
        if not message.id:
            message.id = str(uuid.uuid4())
        row = conn.execute(
            """
                INSERT INTO comms_messages (
                    id, channel_id, identity_id, direction, content, content_type,
                    platform_message_id, platform_thread_id, session_id, status,
                    error, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        return CommsMessage.from_row(row), inserted

    def session_exists(self, session_id: str) -> bool:
        """Whether a session row has exactly this id (no prefix or external-id match)."""
        try:
            session_uuid = uuid.UUID(session_id)
        except ValueError:
            return False
        row = self.db.fetchone("SELECT 1 FROM sessions WHERE id = %s", (session_uuid,))
        return row is not None

    def get_message(self, message_id: str) -> CommsMessage | None:
        """Get a message by ID."""
        row = self.db.fetchone("SELECT * FROM comms_messages WHERE id = %s", (message_id,))
        return CommsMessage.from_row(dict(row)) if row else None

    def get_message_by_platform_id(
        self,
        channel_name: str,
        platform_message_id: str,
        *,
        platform_destination: str | None = None,
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
        params: tuple[str, ...] = (channel_name, platform_message_id, platform_message_id)
        if platform_destination is not None:
            sql += " AND m.metadata_json->>'platform_destination' = %s"
            params += (platform_destination,)
        row = self.db.fetchone(
            sql,
            params,
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
      AND NOT EXISTS (
          SELECT 1
          FROM comms_attachments
          WHERE comms_attachments.message_id = comms_messages.id
            AND comms_attachments.machine_id <> %s
      )
    ORDER BY created_at ASC
    LIMIT %s
), attachment_paths AS MATERIALIZED (
    SELECT local_path
    FROM comms_attachments
    WHERE message_id IN (SELECT id FROM messages_to_delete)
      AND machine_id = %s
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
                (cutoff_value, self.machine_id, limit, self.machine_id),
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

    # Callback keyboard decisions: metadata_json.callback_state is absent while the
    # decision is pending and becomes "answered" or "superseded". callback_generation
    # names the keyboard Telegram should show; every change to it is a CAS on the
    # generation the caller observed, made before the matching Telegram publish.

    def accept_callback_decision(
        self, decision_id: str, generation: int, callback: CommsMessage
    ) -> CommsMessage | None:
        """Answer a pending decision, persist its callback and record its delivery, atomically.

        The answer gets one mailbox row under the callback row's ID, from the system
        session with the Telegram sender in its metadata, and is stamped with who
        consumes it (``CommsMessage.answer_delivery``). A comms asking session has no
        mailbox reader, so its answer starts ``pending`` in the responder's
        ``DecisionAnswerStore`` ledger; any other asker reads its mailbox after the
        caller wakes it. The decision records the answer as ``callback_answer_id``.
        Returns None, persisting nothing, unless the decision is still pending at
        ``generation``. A persistence failure rolls the answer back.
        """
        ensure_system_session(self.db)
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE comms_messages
                   SET metadata_json = jsonb_set(metadata_json, '{callback_state}', '"answered"')
                 WHERE id = %s
                   AND NOT (metadata_json ? 'callback_state')
                   AND COALESCE((metadata_json->>'callback_generation')::int, 0) = %s
                RETURNING session_id
                """,
                (decision_id, generation),
            ).fetchone()
            if row is None:
                return None
            asker = (
                conn.execute(
                    "SELECT id, source FROM sessions WHERE id = %s", (row["session_id"],)
                ).fetchone()
                if row["session_id"]
                else None
            )
            if asker is None:
                return self._insert_message(conn, callback)[0]
            stamp: dict[str, Any] = (
                {"answer_delivery": "responder", "answer_outcome": "pending", "answer_attempt": 1}
                if asker["source"] == "comms"
                else {"answer_delivery": "mailbox"}
            )
            answer = replace(callback, metadata_json={**callback.metadata_json, **stamp})
            persisted, _ = self._insert_message(conn, answer)
            conn.execute(
                """UPDATE comms_messages
                      SET metadata_json = jsonb_set(metadata_json, '{callback_answer_id}',
                                                    to_jsonb(%s::text))
                    WHERE id = %s""",
                (persisted.id, decision_id),
            )
            InterSessionMessageManager(self.db).create_message(
                from_session=system_session_id(),
                to_session=str(asker["id"]),
                content=f"Telegram decision {decision_id} was answered: {persisted.content}",
                message_type="telegram_message",
                metadata_json=json.dumps(_decision_answer_metadata(decision_id, persisted)),
                message_id=persisted.id,
            )
        return persisted

    def claim_callback_reissue(self, message_id: str, generation: int) -> bool:
        """Advance a pending decision's keyboard generation if it is still ``generation``."""
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE comms_messages
                   SET metadata_json = jsonb_set(
                           metadata_json, '{callback_generation}', to_jsonb(%s::int + 1)
                       )
                 WHERE id = %s
                   AND NOT (metadata_json ? 'callback_state')
                   AND COALESCE((metadata_json->>'callback_generation')::int, 0) = %s
                RETURNING id
                """,
                (generation, message_id, generation),
            ).fetchone()
        return row is not None

    def stage_callback_edit(
        self,
        message_id: str,
        generation: int,
        content: str,
        keyboard: list[list[dict[str, str]]] | None,
    ) -> bool:
        """Record an edit as ``generation + 1`` before it is published; False when stale.

        A new keyboard replaces the buttons of a pending decision only; a closed one
        is never reopened. An edit without a keyboard supersedes a pending decision,
        because Telegram drops buttons an edit does not resend.
        """
        keyboard_json = None if keyboard is None else json.dumps(keyboard)
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE comms_messages
                   SET content = %s,
                       metadata_json = CASE
                           WHEN %s::jsonb IS NOT NULL THEN
                               metadata_json || jsonb_build_object('inline_keyboard', %s::jsonb)
                           WHEN metadata_json ? 'callback_state' THEN metadata_json
                           ELSE metadata_json || '{"callback_state": "superseded"}'::jsonb
                       END || jsonb_build_object('callback_generation', %s::int + 1)
                 WHERE id = %s
                   AND COALESCE((metadata_json->>'callback_generation')::int, 0) = %s
                   AND (%s::jsonb IS NULL OR NOT metadata_json ? 'callback_state')
                RETURNING id
                """,
                (
                    content,
                    keyboard_json,
                    keyboard_json,
                    generation,
                    message_id,
                    generation,
                    keyboard_json,
                ),
            ).fetchone()
        return row is not None

    def record_platform_message_ids(self, message_id: str, platform_message_ids: list[str]) -> None:
        """Record the chunk IDs a published edit left the message on."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE comms_messages
                   SET metadata_json = jsonb_set(
                           metadata_json, '{platform_message_ids}', %s::jsonb
                       )
                 WHERE id = %s
                """,
                (json.dumps(platform_message_ids), message_id),
            )

    def restore_callback_edit(self, previous: CommsMessage, staged_generation: int) -> bool:
        """Undo a staged edit that Telegram refused; False when the row moved on."""
        restored = {
            key: previous.metadata_json[key]
            for key in ("inline_keyboard", "callback_state", "callback_generation")
            if key in previous.metadata_json
        }
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE comms_messages
                   SET content = %s,
                       metadata_json = (
                           metadata_json - 'inline_keyboard' - 'callback_state'
                           - 'callback_generation'
                       ) || %s::jsonb
                 WHERE id = %s
                   AND COALESCE((metadata_json->>'callback_generation')::int, 0) = %s
                RETURNING id
                """,
                (previous.content, json.dumps(restored), previous.id, staged_generation),
            ).fetchone()
        return row is not None

    # --- Routing Rules ---

    # --- Attachments ---

    def _insert_attachment(self, conn: Transaction, attachment: CommsAttachment) -> None:
        attachment.machine_id = self.machine_id
        row = conn.execute(
            """
            INSERT INTO comms_attachments (
                id, machine_id, message_id, filename, content_type, size_bytes,
                local_path, platform_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING created_at
            """,
            (
                attachment.id,
                attachment.machine_id,
                attachment.message_id,
                attachment.filename,
                attachment.content_type,
                attachment.size_bytes,
                attachment.local_path,
                attachment.platform_url,
            ),
        ).fetchone()
        if row is not None:
            attachment.created_at = row["created_at"]

    def create_attachment(self, attachment: CommsAttachment) -> CommsAttachment:
        """Save a new attachment to the database."""
        if not attachment.id:
            attachment.id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            self._insert_attachment(conn, attachment)
        return attachment

    def get_attachment(self, attachment_id: str) -> CommsAttachment | None:
        """Get an attachment by ID."""
        row = self.db.fetchone(
            "SELECT * FROM comms_attachments WHERE id = %s AND machine_id = %s",
            (attachment_id, self.machine_id),
        )
        return CommsAttachment.from_row(dict(row)) if row else None

    def list_attachments(self, message_id: str) -> list[CommsAttachment]:
        """List all attachments for a message."""
        rows = self.db.fetchall(
            """SELECT * FROM comms_attachments
               WHERE message_id = %s AND machine_id = %s
               ORDER BY created_at""",
            (message_id, self.machine_id),
        )
        return [CommsAttachment.from_row(dict(row)) for row in rows]

    def delete_attachment(self, attachment_id: str) -> None:
        """Delete an attachment by ID."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM comms_attachments WHERE id = %s AND machine_id = %s",
                (attachment_id, self.machine_id),
            )

    def delete_attachments_for_message(self, message_id: str) -> int:
        """Delete all attachments for a message."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM comms_attachments WHERE message_id = %s AND machine_id = %s",
                (message_id, self.machine_id),
            )
            return cursor.rowcount


def _decision_answer_metadata(decision_id: str, answer: CommsMessage) -> dict[str, Any]:
    """Describe a decision answer's mailbox row: who clicked what, on which decision.

    Only a mailbox-read answer requests a wake; wake recovery cannot reach a comms session.
    """
    return {
        "wake_requested": answer.answer_delivery == "mailbox",
        "comms_decision_id": decision_id,
        "comms_answer_id": answer.id,
        "comms_channel_id": answer.channel_id,
        "comms_identity_id": answer.identity_id,
        "sender": answer.metadata_json.get("external_user_id"),
        "sender_username": answer.metadata_json.get("external_username"),
        "telegram_chat_id": answer.metadata_json.get("chat_id"),
        "callback_data": answer.metadata_json.get("callback_value"),
    }
