"""Communications manager public facade."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from gobby.communications.adapters import get_adapter_class
from gobby.communications.attachments import AttachmentManager
from gobby.communications.identities import IdentityManager
from gobby.communications.inbound import InboundCommunications
from gobby.communications.lifecycle import AdapterLifecycleOperations
from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
    CommsRoutingRule,
)
from gobby.communications.outbound import OutboundCommunications
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


def _lookup_adapter_class(channel_type: str) -> type[BaseChannelAdapter] | None:
    """Compatibility wrapper for tests patching this module's get_adapter_class."""
    return get_adapter_class(channel_type)


class CommunicationsManager:
    """Central manager for communication channel adapters.

    Owns the public communications API while focused helper objects carry
    adapter lifecycle, inbound, and outbound coordination.
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
        self._channel_init_errors: dict[str, str] = {}
        self._websocket_broadcast: Any | None = None

        self._identity_manager = IdentityManager(store, session_store, config)
        self._thread_manager = ThreadManager(max_size=10000)

        self.attachment_manager = AttachmentManager()
        self._rate_limiter = TokenBucketRateLimiter.from_defaults(config.channel_defaults)
        self._router = MessageRouter(store)
        self._polling_manager = PollingManager(self)

        self._lifecycle = AdapterLifecycleOperations(self)
        self._outbound = OutboundCommunications(self)
        self._inbound = InboundCommunications(self)

        self.event_callback: Callable[..., Any] | None = None
        self.reaction_handler: Any | None = None

    @property
    def store(self) -> LocalCommunicationsStore:
        """Storage backend for integrations that need communications persistence."""
        return self._store

    def _get_thread_id(self, channel_id: str, session_id: str) -> str | None:
        return self._thread_manager.get_thread_id(channel_id, session_id)

    def _track_thread(self, channel_id: str, session_id: str, platform_thread_id: str) -> None:
        self._thread_manager.track_thread(channel_id, session_id, platform_thread_id)

    async def start(self) -> None:
        """Load enabled channels from DB, initialize adapters, configure rate limiter."""
        await self._lifecycle.start()

    async def stop(self) -> None:
        """Shutdown all adapters and clear state."""
        await self._lifecycle.stop()

    async def _init_adapter(self, channel: ChannelConfig) -> BaseChannelAdapter:
        """Lookup adapter class from registry, instantiate, and initialize."""
        return await self._lifecycle.init_adapter(channel)

    async def _enrich_outbound_metadata(
        self,
        channel: ChannelConfig,
        channel_name: str,
        session_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build effective metadata for outbound messages/attachments."""
        return await self._outbound.enrich_metadata(channel, channel_name, session_id, metadata)

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommsMessage:
        """Send a message to a named channel."""
        return await self._outbound.send_message(channel_name, content, session_id, metadata)

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
        return await self._outbound.send_attachment(
            channel_name,
            file_path,
            filename,
            content_type,
            content,
            session_id,
            metadata,
        )

    async def send_event(
        self,
        event_type: str,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> list[CommsMessage]:
        """Route event to matching channels and send to each."""
        return await self._outbound.send_event(event_type, content, project_id, session_id)

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
        """Process, resolve identity, and store inbound messages."""
        return await self._inbound.handle_messages(channel_name, messages)

    async def handle_inbound(
        self,
        channel_name: str,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
        raw_body: bytes | None = None,
    ) -> list[CommsMessage]:
        """Handle an inbound webhook payload."""
        return await self._inbound.handle_webhook(channel_name, payload, headers, raw_body)

    async def add_channel(
        self,
        channel_type: str,
        name: str,
        config: dict[str, Any],
        secrets: dict[str, Any] | None = None,
    ) -> ChannelConfig:
        """Create a new channel in DB and initialize its adapter."""
        return await self._lifecycle.add_channel(channel_type, name, config, secrets)

    async def remove_channel(self, name: str) -> None:
        """Shutdown adapter and delete channel from DB."""
        await self._lifecycle.remove_channel(name)

    async def _ensure_gobby_chat_channel(self) -> None:
        """Auto-create a gobby_chat channel if one doesn't already exist."""
        await self._lifecycle.ensure_gobby_chat_channel()

    def set_websocket_broadcast(self, broadcast: Any) -> None:
        """Wire the WebSocket broadcast callable into the gobby_chat adapter."""
        self._websocket_broadcast = broadcast
        self._lifecycle.set_websocket_broadcast(broadcast)

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        """Get a channel by ID."""
        return self._store.get_channel(channel_id)

    async def update_channel(self, channel: ChannelConfig) -> ChannelConfig:
        """Update channel configuration in DB."""
        return await self._lifecycle.update_channel(channel)

    def channel_to_dict(self, channel: ChannelConfig) -> dict[str, Any]:
        """Serialize a channel with runtime activity and initialization state."""
        payload = asdict(channel)
        payload.pop("webhook_secret", None)
        payload["active"] = channel.name in self._adapters
        payload["init_error"] = self._channel_init_errors.get(channel.name)
        return payload

    def list_channels(self) -> list[ChannelConfig]:
        """List all channels (enabled and disabled) from DB."""
        return self._store.list_channels(enabled_only=False)

    def get_channel_status(self, name: str) -> dict[str, Any]:
        """Get adapter health/connected status for a channel."""
        return self._lifecycle.get_channel_status(name)

    def get_channel_by_name(self, name: str) -> ChannelConfig | None:
        """Get a channel by name."""
        return self._store.get_channel_by_name(name)

    async def send_proactive(
        self, channel_name: str, conversation_id: str, content: str, content_type: str = "text"
    ) -> CommsMessage:
        """Send a proactive message via an adapter that supports it."""
        return await self._outbound.send_proactive(
            channel_name, conversation_id, content, content_type
        )

    def list_messages(
        self,
        channel_id: str | None = None,
        session_id: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CommsMessage]:
        """List messages with optional filters."""
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
        """Get an identity by channel and external user ID."""
        return self._store.get_identity_by_external(channel_id, external_user_id)

    def list_identities(self, channel_id: str | None = None) -> list[CommsIdentity]:
        """List identities, optionally filtered by channel."""
        return self._store.list_identities(channel_id=channel_id)

    def update_identity_session(self, identity_id: str, session_id: str | None) -> None:
        """Link or unlink an identity to a session."""
        self._store.update_identity_session(identity_id, session_id)

    def create_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Create a routing rule and invalidate the router cache."""
        result = self._store.create_routing_rule(rule)
        self._router.invalidate_cache()
        return result

    def update_routing_rule(self, rule: CommsRoutingRule) -> CommsRoutingRule:
        """Update a routing rule and invalidate the router cache."""
        result = self._store.update_routing_rule(rule)
        self._router.invalidate_cache()
        return result

    def delete_routing_rule(self, rule_id: str) -> None:
        """Delete a routing rule and invalidate the router cache."""
        self._store.delete_routing_rule(rule_id)
        self._router.invalidate_cache()
