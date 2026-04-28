"""Central CommunicationsManager for the Gobby communications framework."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.communications.adapters import get_adapter_class
from gobby.communications.attachments import AttachmentManager
from gobby.communications.identities import IdentityManager
from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.communications.polling import PollingManager
from gobby.communications.rate_limiter import TokenBucketRateLimiter
from gobby.communications.router import MessageRouter
from gobby.communications.threads import ThreadManager

if TYPE_CHECKING:
    from pathlib import Path

    from gobby.communications.adapters.base import BaseChannelAdapter
    from gobby.config.communications import CommunicationsConfig
    from gobby.storage.communications import LocalCommunicationsStore
    from gobby.storage.secrets import SecretStore
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


class CommunicationsManager:
    """Central manager for communication channel adapters.

    Owns the adapter lifecycle, message routing, and inbound/outbound coordination.
    Same pattern as pipeline_executor, memory_manager, etc. on ServiceContainer.
    """

    def __init__(
        self,
        config: CommunicationsConfig,
        store: LocalCommunicationsStore,
        secret_store: SecretStore,
        session_store: SessionManager,
    ) -> None:
        """Initialize the communications manager.

        Args:
            config: Communications configuration.
            store: Local communications storage manager.
            secret_store: Secret store for resolving $secret: references.
            session_store: Session store for creating auto-sessions.
        """
        self._config = config
        self._store = store
        self._secret_store = secret_store
        self._session_store = session_store
        self._adapters: dict[str, BaseChannelAdapter] = {}
        self._channel_by_name: dict[str, ChannelConfig] = {}

        # Extracted managers
        self._identity_manager = IdentityManager(store, session_store, config)
        self._thread_manager = ThreadManager(max_size=10000)

        self.attachment_manager = AttachmentManager()
        self._rate_limiter = TokenBucketRateLimiter.from_defaults(config.channel_defaults)
        self._router = MessageRouter(store)
        self._polling_manager = PollingManager(self)
        self.event_callback: Callable[..., Any] | None = None
        self.reaction_handler: Any | None = None

    def _get_thread_id(self, channel_name: str, session_id: str) -> str | None:
        return self._thread_manager.get_thread_id(channel_name, session_id)

    def _track_thread(self, channel_name: str, session_id: str, platform_thread_id: str) -> None:
        self._thread_manager.track_thread(channel_name, session_id, platform_thread_id)

    async def start(self) -> None:
        """Load enabled channels from DB, initialize adapters, configure rate limiter."""
        # Auto-create gobby_chat channel if none exists
        await self._ensure_gobby_chat_channel()

        channels = self._store.list_channels(enabled_only=True)
        for channel in channels:
            try:
                adapter = await self._init_adapter(channel)
                self._adapters[channel.name] = adapter
                self._channel_by_name[channel.name] = channel
                # Configure rate limiter with per-channel overrides
                rate = channel.config_json.get(
                    "rate_limit_per_minute",
                    self._config.channel_defaults.rate_limit_per_minute,
                )
                burst = channel.config_json.get(
                    "burst",
                    self._config.channel_defaults.burst,
                )
                self._rate_limiter.configure_channel(channel.id, int(rate), int(burst))

                # Start polling if supported and no webhook URL configured
                if adapter.supports_polling and not self._config.webhook_base_url:
                    interval = channel.config_json.get("poll_interval")
                    self._polling_manager.start_polling(channel.name, adapter, interval)

                logger.info(
                    f"Communications: initialized channel {channel.name!r} ({channel.channel_type})",
                )
            except Exception as e:
                logger.error(f"Failed to initialize channel {channel.name!r}: {e}", exc_info=True)
        logger.info(f"CommunicationsManager started ({len(self._adapters)} channels active)")

    async def stop(self) -> None:
        """Shutdown all adapters and clear state."""
        self._polling_manager.stop_all()
        for name, adapter in list(self._adapters.items()):
            try:
                await adapter.shutdown()
            except Exception as e:
                logger.error(f"Error shutting down channel {name!r}: {e}", exc_info=True)
        self._adapters.clear()
        self._channel_by_name.clear()
        logger.info("CommunicationsManager stopped")

    async def _init_adapter(self, channel: ChannelConfig) -> BaseChannelAdapter:
        """Lookup adapter class from registry, instantiate, and initialize.

        Args:
            channel: Channel configuration.

        Returns:
            Initialized adapter instance.

        Raises:
            ValueError: If no adapter is registered for the channel type.
        """
        adapter_cls = get_adapter_class(channel.channel_type)
        if adapter_cls is None:
            raise ValueError(f"No adapter registered for channel type {channel.channel_type!r}")
        adapter = adapter_cls()

        # Wire rate limiter backoff to the adapter
        def on_rate_limit(duration: float, is_global: bool = False, cid: str = channel.id) -> None:
            if is_global:
                # Back off all channels of the same type
                for c in self._channel_by_name.values():
                    if c.channel_type == channel.channel_type:
                        self._rate_limiter.set_backoff(c.id, duration)
                logger.warning(
                    "Global rate limit hit on %s, backing off ALL %s channels for %.1fs",
                    channel.name,
                    channel.channel_type,
                    duration,
                )
            else:
                self._rate_limiter.set_backoff(cid, duration)

        adapter.set_rate_limit_callback(on_rate_limit)
        await adapter.initialize(channel, self._secret_store.get)
        return adapter

    def _enrich_outbound_metadata(
        self,
        channel: ChannelConfig,
        channel_name: str,
        session_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build effective metadata for outbound messages/attachments."""
        effective = dict(metadata) if metadata else {}
        if "platform_destination" not in effective:
            default_dest = channel.config_json.get("default_destination")
            if default_dest:
                effective["platform_destination"] = default_dest

            if not effective.get("platform_destination") and session_id:
                identity = self._identity_manager.get_identity_by_session(channel.id, session_id)
                if identity and "conversation_reference" in identity.metadata_json:
                    effective["conversation_reference"] = identity.metadata_json[
                        "conversation_reference"
                    ]
                    logger.debug(
                        "Injected conversation_reference for proactive messaging on %s",
                        channel_name,
                    )
        return effective

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommsMessage:
        """Send a message to a named channel.

        Args:
            channel_name: Name of the channel to send to.
            content: Message content.
            session_id: Optional session ID to associate with the message.
            metadata: Optional metadata dict.

        Returns:
            The CommsMessage that was sent (or failed).

        Raises:
            ValueError: If the channel is not found or not active.
        """
        adapter = self._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = self._channel_by_name[channel_name]

        # Rate limit check — waits until a token is available
        await self._rate_limiter.wait_if_needed(channel.id)

        # Look up thread if we have a session
        platform_thread_id = None
        if session_id:
            platform_thread_id = self._get_thread_id(channel_name, session_id)

        effective_metadata = self._enrich_outbound_metadata(
            channel, channel_name, session_id, metadata
        )

        # Build CommsMessage
        message = CommsMessage(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            direction="outbound",
            content=content,
            session_id=session_id,
            status="pending",
            platform_thread_id=platform_thread_id,
            metadata_json=effective_metadata,
            created_at=datetime.now(UTC).isoformat(),
        )

        # Send via adapter
        try:
            platform_message_id = await adapter.send_message(message)
            message.platform_message_id = platform_message_id
            message.status = "sent"
        except Exception as e:
            message.status = "failed"
            message.error = str(e)
            logger.error(f"Failed to send message to {channel_name!r}: {e}", exc_info=True)

        # Store in DB
        try:
            self._store.create_message(message)
        except Exception as e:
            logger.error(f"Failed to store outbound message: {e}", exc_info=True)

        # Fire event callback
        if self.event_callback is not None:
            try:
                await self.event_callback("comms.message_sent", message=message)
            except Exception as e:
                logger.debug(f"Event callback error on send_message: {e}", exc_info=True)

        return message

    async def send_attachment(
        self,
        channel_name: str,
        file_path: Path,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
        content: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[CommsMessage, CommsAttachment]:
        """Send a file attachment to a named channel."""
        from pathlib import Path as PathType

        file_path = PathType(file_path)
        if not file_path.exists():
            raise ValueError(f"Attachment file not found: {file_path}")

        adapter = self._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = self._channel_by_name[channel_name]
        size_bytes = file_path.stat().st_size

        if not self.attachment_manager.validate_size(size_bytes, channel.channel_type):
            limit = self.attachment_manager.get_size_limit(channel.channel_type)
            raise ValueError(
                f"File size {size_bytes} exceeds {channel.channel_type} limit of {limit} bytes"
            )

        await self._rate_limiter.wait_if_needed(channel.id)

        platform_thread_id = None
        if session_id:
            platform_thread_id = self._get_thread_id(channel_name, session_id)

        display_name = filename or file_path.name

        effective_metadata = self._enrich_outbound_metadata(
            channel, channel_name, session_id, metadata
        )

        message = CommsMessage(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            direction="outbound",
            content=content,
            content_type="attachment",
            session_id=session_id,
            status="pending",
            platform_thread_id=platform_thread_id,
            metadata_json=effective_metadata,
            created_at=datetime.now(UTC).isoformat(),
        )

        attachment = CommsAttachment(
            id=str(uuid.uuid4()),
            message_id=message.id,
            filename=display_name,
            content_type=content_type,
            size_bytes=size_bytes,
            local_path=str(file_path),
            created_at=datetime.now(UTC).isoformat(),
        )

        try:
            platform_message_id = await adapter.send_attachment(message, attachment, file_path)
            message.platform_message_id = platform_message_id
            message.status = "sent"
        except NotImplementedError:
            message.status = "failed"
            message.error = f"{channel.channel_type} adapter does not support file attachments"
            logger.error(f"Adapter {channel_name!r} does not support attachments")
        except Exception as e:
            message.status = "failed"
            message.error = str(e)
            logger.error(f"Failed to send attachment to {channel_name!r}: {e}", exc_info=True)

        try:
            self._store.create_message(message)
            self._store.create_attachment(attachment)
        except Exception as e:
            logger.error(f"Failed to store outbound attachment: {e}", exc_info=True)

        if self.event_callback is not None:
            try:
                await self.event_callback(
                    "comms.attachment_sent", message=message, attachment=attachment
                )
            except Exception as e:
                logger.debug(f"Event callback error on send_attachment: {e}", exc_info=True)

        return message, attachment

    async def send_event(
        self,
        event_type: str,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> list[CommsMessage]:
        """Route event to matching channels and send to each.

        Args:
            event_type: Event type string (e.g., "task.created").
            content: Message content to send.
            project_id: Optional project ID for routing rule matching.
            session_id: Optional session ID for routing rule matching.

        Returns:
            List of CommsMessages sent.
        """
        channel_ids = await self._router.match_channels(
            event_type, project_id=project_id, session_id=session_id
        )

        # Build reverse map: channel_id -> channel_name for active adapters
        id_to_name: dict[str, str] = {c.id: n for n, c in self._channel_by_name.items()}

        messages: list[CommsMessage] = []
        for channel_id in channel_ids:
            channel_name = id_to_name.get(channel_id)
            if channel_name is None:
                continue
            try:
                msg = await self.send_message(channel_name, content, session_id=session_id)
                messages.append(msg)
            except Exception as e:
                logger.error(f"send_event: failed to send to {channel_name!r}: {e}")

        return messages

    def _find_cross_channel_identity(self, external_username: str) -> str | None:
        """Search for matching identity on other channels by username pattern."""
        return self._identity_manager.find_cross_channel_identity(external_username)

    def _bridge_identity(self, identity_id: str, session_id: str) -> None:
        """Link existing identity to a session."""
        self._identity_manager.bridge_identity(identity_id, session_id)

    async def _resolve_identity(
        self, channel_id: str, external_user_id: str, external_username: str | None = None
    ) -> CommsIdentity:
        """Resolve identity and auto-create/link session if needed."""
        return self._identity_manager.resolve_identity(
            channel_id, external_user_id, external_username
        )

    async def handle_inbound_messages(
        self, channel_name: str, messages: list[CommsMessage]
    ) -> list[CommsMessage]:
        """Process, resolve identity, and store a list of inbound messages.

        Args:
            channel_name: Name of the channel the messages came from.
            messages: List of parsed CommsMessage objects.

        Returns:
            List of stored CommsMessages.

        Raises:
            ValueError: If channel is not found or not active.
        """
        channel = self._channel_by_name.get(channel_name)
        if channel is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        stored: list[CommsMessage] = []
        for message in messages:
            if message.content_type == "reaction":
                if self.reaction_handler:
                    try:
                        await self.reaction_handler.handle_reaction(
                            channel_name,
                            message.platform_message_id,
                            message.content,
                            message.identity_id,
                        )
                    except Exception as e:
                        logger.error(f"Failed to handle reaction: {e}", exc_info=True)
                continue

            # Ensure channel_id is set
            if not message.channel_id:
                message.channel_id = channel.id

            # Resolve identity: if identity_id looks like an external_user_id, resolve it
            if message.identity_id:
                external_username = message.metadata_json.get("external_username")
                # Capture conversation_reference from message metadata if present (proactive messaging)
                identity_meta = {}
                if "conversation_reference" in message.metadata_json:
                    identity_meta["conversation_reference"] = message.metadata_json[
                        "conversation_reference"
                    ]

                identity = self._identity_manager.resolve_identity(
                    channel.id, message.identity_id, external_username, metadata=identity_meta
                )
                message.session_id = identity.session_id
                message.identity_id = identity.id

            if message.session_id and message.platform_thread_id:
                self._track_thread(channel_name, message.session_id, message.platform_thread_id)

            # Store message
            try:
                self._store.create_message(message)
                stored.append(message)
            except Exception as e:
                logger.error(f"Failed to store inbound message: {e}")

        # Fire event callback for each stored message
        if self.event_callback is not None:
            for msg in stored:
                try:
                    await self.event_callback("comms.message_received", message=msg)
                except Exception as e:
                    logger.debug(f"Event callback error on handle_inbound_messages: {e}")

        return stored

    async def handle_inbound(
        self,
        channel_name: str,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
        raw_body: bytes | None = None,
    ) -> list[CommsMessage]:
        """Handle an inbound webhook payload.

        Args:
            channel_name: Name of the channel receiving the webhook.
            payload: Raw webhook payload (dict or bytes).
            headers: HTTP headers from the webhook request.

        Returns:
            List of stored CommsMessages parsed from the payload.

        Raises:
            ValueError: If channel is not found or webhook verification fails.
        """
        adapter = self._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = self._channel_by_name[channel_name]

        # Verify webhook signature if a secret is configured
        if channel.webhook_secret:
            verify_bytes: bytes
            if raw_body is not None:
                verify_bytes = raw_body
            elif isinstance(payload, bytes):
                verify_bytes = payload
            else:
                # We cannot safely verify signature without raw bytes, as JSON serialization
                # might differ from the original request body, breaking HMAC.
                raise ValueError("raw_body must be provided for webhook signature verification")

            if not adapter.verify_webhook(verify_bytes, headers, channel.webhook_secret):
                raise ValueError(
                    f"Webhook signature verification failed for channel {channel_name!r}"
                )

        # Parse messages from payload
        payload_for_parse: dict[str, Any] | bytes = payload
        parsed: list[CommsMessage] = adapter.parse_webhook(payload_for_parse, headers)

        # If any message is a URL verification challenge, return immediately without storing
        for msg in parsed:
            if msg.content_type == "url_verification":
                if not msg.channel_id:
                    msg.channel_id = channel.id
                return parsed

        return await self.handle_inbound_messages(channel_name, parsed)

    async def add_channel(
        self,
        channel_type: str,
        name: str,
        config: dict[str, Any],
        secrets: dict[str, Any] | None = None,
    ) -> ChannelConfig:
        """Create a new channel in DB and initialize its adapter.

        Args:
            channel_type: The type identifier (e.g., "slack", "telegram").
            name: Unique channel name.
            config: Channel configuration dict.
            secrets: Optional secrets dict (may include webhook_secret).

        Returns:
            The created ChannelConfig.
        """
        now = datetime.now(UTC).isoformat()

        # Store secrets in SecretStore and replace with $secret: references
        if secrets:
            for key, value in secrets.items():
                if key == "webhook_secret":
                    continue  # Handled via channel_config field
                if not value:
                    continue  # Skip empty values
                # Channel-scoped secret name avoids collisions between channels
                secret_name = f"COMMS_{channel_type.upper()}_{key.upper()}_{name.upper()}"
                self._secret_store.set(
                    name=secret_name,
                    plaintext_value=str(value),
                    category="comms",
                    description=f"{channel_type} channel '{name}': {key}",
                )
                # Put reference in config so adapter resolves it
                config[key] = f"$secret:{secret_name}"

        channel_config = ChannelConfig(
            id=str(uuid.uuid4()),
            channel_type=channel_type,
            name=name,
            enabled=True,
            config_json=config,
            created_at=now,
            updated_at=now,
            webhook_secret=secrets.get("webhook_secret") if secrets else None,
        )

        # Save to DB
        self._store.create_channel(channel_config)

        # Initialize adapter
        try:
            adapter = await self._init_adapter(channel_config)
            self._adapters[name] = adapter
            self._channel_by_name[name] = channel_config
            # Configure rate limiter
            rate = config.get(
                "rate_limit_per_minute",
                self._config.channel_defaults.rate_limit_per_minute,
            )
            burst = config.get(
                "burst",
                self._config.channel_defaults.burst,
            )
            self._rate_limiter.configure_channel(channel_config.id, int(rate), int(burst))

            # Also start polling if newly added channel supports it
            if adapter.supports_polling and not self._config.webhook_base_url:
                interval = channel_config.config_json.get("poll_interval")
                self._polling_manager.start_polling(name, adapter, interval)

            logger.info(f"Added channel {name!r} ({channel_type})")
        except Exception as e:
            logger.error(f"Failed to initialize adapter for new channel {name!r}: {e}")

        return channel_config

    async def remove_channel(self, name: str) -> None:
        """Shutdown adapter and delete channel from DB.

        Args:
            name: Channel name to remove.
        """
        adapter = self._adapters.pop(name, None)
        channel = self._channel_by_name.pop(name, None)

        if adapter is not None:
            self._polling_manager.stop_polling(name)
            try:
                await adapter.shutdown()
            except Exception as e:
                logger.error(f"Error shutting down channel {name!r}: {e}", exc_info=True)

        if channel is not None:
            self._rate_limiter.remove_channel(channel.id)
            try:
                self._store.delete_channel(channel.id)
                logger.info(f"Removed channel {name!r}")
            except Exception as e:
                logger.error(f"Failed to delete channel {name!r} from DB: {e}")

    async def _ensure_gobby_chat_channel(self) -> None:
        """Auto-create a gobby_chat channel if one doesn't already exist.

        The gobby_chat channel is an internal bridge that allows routing
        rules to target the web UI.  Unlike external channels, it needs
        no credentials.
        """
        channels = self._store.list_channels(enabled_only=False)
        if any(c.channel_type == "gobby_chat" for c in channels):
            return

        if get_adapter_class("gobby_chat") is None:
            logger.debug("gobby_chat adapter not registered, skipping auto-create")
            return

        now = datetime.now(UTC).isoformat()
        channel = ChannelConfig(
            id=str(uuid.uuid4()),
            channel_type="gobby_chat",
            name="gobby_chat",
            enabled=True,
            config_json={},
            created_at=now,
            updated_at=now,
        )
        try:
            self._store.create_channel(channel)
            logger.info("Auto-created gobby_chat channel for unified routing")
        except Exception as e:
            logger.error(f"Failed to auto-create gobby_chat channel: {e}", exc_info=True)

    def set_websocket_broadcast(self, broadcast: Any) -> None:
        """Wire the WebSocket broadcast callable into the gobby_chat adapter.

        Called by GobbyRunner after both CommunicationsManager and
        WebSocketServer are initialized.

        Args:
            broadcast: The WebSocketServer.broadcast async method.
        """
        from gobby.communications.adapters.gobby_chat import GobbyChatAdapter

        adapter = self._adapters.get("gobby_chat")
        if isinstance(adapter, GobbyChatAdapter):
            adapter.set_broadcast(broadcast)
            logger.info("GobbyChatAdapter wired to WebSocket broadcast")

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        """Get a channel by ID.

        Args:
            channel_id: The channel UUID.

        Returns:
            ChannelConfig if found, None otherwise.
        """
        return self._store.get_channel(channel_id)

    def update_channel(self, channel: ChannelConfig) -> ChannelConfig:
        """Update channel configuration in DB.

        Args:
            channel: The ChannelConfig with updated fields.

        Returns:
            The updated ChannelConfig.
        """
        channel.updated_at = datetime.now(UTC).isoformat()
        return self._store.update_channel(channel)

    def list_channels(self) -> list[ChannelConfig]:
        """List all channels (enabled and disabled) from DB.

        Returns:
            List of ChannelConfig objects.
        """
        return self._store.list_channels(enabled_only=False)

    def get_channel_status(self, name: str) -> dict[str, Any]:
        """Get adapter health/connected status for a channel.

        Args:
            name: Channel name.

        Returns:
            Dict with status information.
        """
        channel = self._channel_by_name.get(name)
        adapter = self._adapters.get(name)

        if channel is not None and adapter is not None:
            return {
                "name": name,
                "channel_type": channel.channel_type,
                "status": "active",
                "active": True,
                "enabled": channel.enabled,
                "supports_webhooks": adapter.supports_webhooks,
                "supports_polling": adapter.supports_polling,
                "is_polling": self._polling_manager.is_polling(name),
            }

        # Channel not active — check DB
        channels = self._store.list_channels(enabled_only=False)
        db_channel = next((c for c in channels if c.name == name), None)
        if db_channel is None:
            return {"name": name, "status": "not_found", "active": False}

        return {
            "name": name,
            "channel_type": db_channel.channel_type,
            "status": "inactive",
            "active": False,
            "enabled": db_channel.enabled,
        }

    # --- Public store delegation methods ---

    def get_channel_by_name(self, name: str) -> ChannelConfig | None:
        """Get a channel by name.

        Args:
            name: The channel name.

        Returns:
            ChannelConfig if found, None otherwise.
        """
        return self._store.get_channel_by_name(name)

    async def send_proactive(
        self, channel_name: str, conversation_id: str, content: str, content_type: str = "text"
    ) -> str | None:
        """Send a proactive message via an adapter that supports it (e.g., Teams).

        Args:
            channel_name: Name of the channel to send through.
            conversation_id: Platform-specific conversation identifier.
            content: Message content.
            content_type: Content type ('text' or 'adaptive_card').

        Returns:
            Platform message ID if successful.

        Raises:
            ValueError: If channel not found or adapter doesn't support proactive messaging.
        """
        adapter = self._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        try:
            return await adapter.send_proactive(conversation_id, content, content_type)
        except NotImplementedError as exc:
            raise ValueError(
                f"Channel {channel_name!r} does not support proactive messaging"
            ) from exc

    def list_messages(
        self,
        channel_id: str | None = None,
        session_id: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CommsMessage]:
        """List messages with optional filters.

        Args:
            channel_id: Filter by channel ID.
            session_id: Filter by session ID.
            direction: Filter by direction ('inbound' or 'outbound').
            limit: Max results to return.
            offset: Number of results to skip.

        Returns:
            List of CommsMessage objects.
        """
        return self._store.list_messages(
            channel_id=channel_id,
            session_id=session_id,
            direction=direction,
            limit=limit,
            offset=offset,
        )

    def get_identity_by_external(
        self, channel_id: str, external_user_id: str
    ) -> CommsIdentity | None:
        """Get an identity by channel and external user ID.

        Args:
            channel_id: The channel UUID.
            external_user_id: The external platform user ID.

        Returns:
            CommsIdentity if found, None otherwise.
        """
        return self._store.get_identity_by_external(channel_id, external_user_id)

    def list_identities(self, channel_id: str | None = None) -> list[CommsIdentity]:
        """List identities, optionally filtered by channel.

        Args:
            channel_id: Optional channel ID filter.

        Returns:
            List of CommsIdentity objects.
        """
        return self._store.list_identities(channel_id=channel_id)

    def update_identity_session(self, identity_id: str, session_id: str | None) -> None:
        """Link or unlink an identity to a session.

        Args:
            identity_id: The identity UUID.
            session_id: Session ID to link, or None to unlink.
        """
        self._store.update_identity_session(identity_id, session_id)

    # --- Routing Rule CRUD (with cache invalidation) ---

    def create_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Create a routing rule and invalidate the router cache.

        Args:
            rule: The routing rule to create.

        Returns:
            The created routing rule.
        """
        result = self._store.create_routing_rule(rule)
        self._router.invalidate_cache()
        return result

    def update_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Update a routing rule and invalidate the router cache.

        Args:
            rule: The routing rule to update.

        Returns:
            The updated routing rule.
        """
        result = self._store.update_routing_rule(rule)
        self._router.invalidate_cache()
        return result

    def delete_routing_rule(self, rule_id: str) -> None:
        """Delete a routing rule and invalidate the router cache.

        Args:
            rule_id: ID of the rule to delete.
        """
        self._store.delete_routing_rule(rule_id)
        self._router.invalidate_cache()
