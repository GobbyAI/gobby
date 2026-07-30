"""Communications manager public facade."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from gobby.communications.adapters import get_adapter_class
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.attachments import AttachmentManager
from gobby.communications.group_policy import evaluate_group_message
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
from gobby.communications.responder import CommunicationsResponder
from gobby.communications.router import MessageRouter
from gobby.communications.telegram_access import (
    allowed_senders,
    is_deliberate_start,
    is_telegram_dm,
    telegram_dm_sender,
)
from gobby.communications.threads import ThreadManager
from gobby.communications.voice import VoiceTranscriber, VoiceTranscriberGetter
from gobby.utils.datetime import utc_now


class EventSubscriptionNotFoundError(LookupError):
    """Raised when an event subscription does not exist."""


class _Unset:
    pass


_UNSET = _Unset()

if TYPE_CHECKING:
    from pathlib import Path

    from gobby.ai.vision import VisionExtractService
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
        self._telegram_binding_locks: dict[str, asyncio.Lock] = {}
        self._websocket_broadcast: Any | None = None
        self._voice_transcriber_getter: VoiceTranscriberGetter | None = None
        self._voice_transcription_timeout_seconds = 120.0
        self._vision_extract_service: VisionExtractService | None = None

        self._identity_manager = IdentityManager(store, session_store, config)
        self._thread_manager = ThreadManager(max_size=10000)

        self.attachment_manager = AttachmentManager()
        self._rate_limiter = TokenBucketRateLimiter.from_defaults(config.channel_defaults)
        self._router = MessageRouter(store)
        self._polling_manager = PollingManager(self)

        self._lifecycle = AdapterLifecycleOperations(self)
        self._outbound = OutboundCommunications(self)
        self._inbound = InboundCommunications(self)
        self.responder = CommunicationsResponder(self)

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
        await self.responder.stop()
        await self._lifecycle.stop()
        if self._vision_extract_service is not None:
            await self._vision_extract_service.stop()

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

    def supports_message_edit(self, channel_name: str) -> bool:
        """Return whether the active adapter implements message editing."""
        adapter = self._adapters.get(channel_name)
        return bool(adapter and adapter.supports_message_edit)

    def supports_typing(self, channel_name: str) -> bool:
        """Return whether the active adapter implements typing indicators."""
        adapter = self._adapters.get(channel_name)
        return bool(adapter and adapter.supports_typing)

    def supports_reactions(self, channel_name: str) -> bool:
        """Return whether the active adapter implements message reactions."""
        adapter = self._adapters.get(channel_name)
        return bool(adapter and adapter.supports_reactions)

    async def send_typing(self, channel_name: str, conversation_id: str) -> None:
        """Publish a typing indicator through an active adapter."""
        adapter = self._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")
        if not self.supports_typing(channel_name):
            raise NotImplementedError(
                f"{adapter.channel_type} adapter does not support typing indicators"
            )
        channel = self._channel_by_name[channel_name]
        await self._rate_limiter.wait_if_needed(channel.id)
        await adapter.send_typing(conversation_id)

    async def set_reaction(
        self,
        channel_name: str,
        conversation_id: str,
        platform_message_id: str,
        reaction: str | None,
    ) -> None:
        """Add or remove a reaction through an active adapter."""
        adapter = self._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")
        if not self.supports_reactions(channel_name):
            raise NotImplementedError(f"{adapter.channel_type} adapter does not support reactions")
        channel = self._channel_by_name[channel_name]
        await self._rate_limiter.wait_if_needed(channel.id)
        await adapter.set_reaction(conversation_id, platform_message_id, reaction)

    async def edit_message(
        self,
        channel_name: str,
        platform_message_id: str,
        content: str,
        conversation_id: str,
    ) -> None:
        """Replace an existing platform message through an active adapter."""
        adapter = self._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")
        if not self.supports_message_edit(channel_name):
            raise NotImplementedError(
                f"{adapter.channel_type} adapter does not support message editing"
            )
        channel = self._channel_by_name[channel_name]
        await self._rate_limiter.wait_if_needed(channel.id)
        await adapter.edit_message(platform_message_id, content, conversation_id)
        stored_message = await asyncio.to_thread(
            self._store.get_message_by_platform_id,
            channel_name,
            platform_message_id,
        )
        if stored_message is not None:
            await asyncio.to_thread(
                self._store.update_message_content,
                stored_message.id,
                content,
            )

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
        *,
        event_id: str | None = None,
    ) -> list[CommsMessage]:
        """Route event to matching channels and send to each."""
        return await self._outbound.send_event(
            event_type,
            content,
            project_id,
            session_id,
            event_id=event_id,
        )

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

    def set_voice_transcriber_getter(
        self,
        getter: VoiceTranscriberGetter | None,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Wire the voice subsystem's current speech-to-text singleton."""
        self._voice_transcriber_getter = getter
        self._voice_transcription_timeout_seconds = timeout_seconds

    def get_voice_transcriber(self) -> VoiceTranscriber | None:
        """Return the current shared speech-to-text service when available."""
        if self._voice_transcriber_getter is None:
            return None
        return self._voice_transcriber_getter()

    @property
    def voice_transcription_timeout_seconds(self) -> float:
        """Return the configured per-message speech-to-text deadline."""
        return self._voice_transcription_timeout_seconds

    def set_vision_extract_service(self, service: VisionExtractService | None) -> None:
        """Wire the daemon's configured vision extraction service."""
        self._vision_extract_service = service

    def get_vision_extract_service(self) -> VisionExtractService | None:
        """Return the configured daemon vision extraction service."""
        return self._vision_extract_service

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        """Get a channel by ID."""
        return self._store.get_channel(channel_id)

    async def admit_inbound_message(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> bool:
        """Apply channel access control before resolving identity or persisting content."""
        if channel.channel_type == "telegram":
            group_decision = evaluate_group_message(channel.config_json, message)
            if group_decision.is_group:
                if not group_decision.authorized:
                    return False
                message.metadata_json["passive_context"] = not group_decision.should_respond
                return True

        if not is_telegram_dm(channel, message):
            return True

        sender_id = telegram_dm_sender(channel, message)
        if sender_id is None:
            return False

        allow_from = allowed_senders(channel.config_json)
        if sender_id in allow_from or "*" in allow_from:
            return True
        if allow_from or not is_deliberate_start(message.content):
            return False

        lock = self._telegram_binding_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            candidate = await asyncio.to_thread(self._store.get_channel, channel.id)
            current = candidate if isinstance(candidate, ChannelConfig) else channel
            current_allow_from = allowed_senders(current.config_json)
            if current_allow_from:
                return sender_id in current_allow_from or "*" in current_allow_from

            config_json = dict(current.config_json)
            config_json["allow_from"] = [sender_id]
            updated = replace(current, config_json=config_json, updated_at=utc_now())
            await asyncio.to_thread(self._store.update_channel, updated)

            channel.config_json = dict(config_json)
            channel.updated_at = updated.updated_at
            cached = self._channel_by_name.get(channel.name)
            if cached is not None and cached is not channel:
                cached.config_json = dict(config_json)
                cached.updated_at = updated.updated_at
            message.metadata_json["telegram_first_contact_bound"] = True
            return True

    async def update_channel(
        self, channel: ChannelConfig, secrets: dict[str, Any] | None = None
    ) -> ChannelConfig:
        """Update channel configuration in DB."""
        return await self._lifecycle.update_channel(channel, secrets=secrets)

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

    def _resolve_subscription_channel(self, channel: str) -> ChannelConfig:
        channel_ref = channel.strip()
        if not channel_ref:
            raise ValueError("Channel is required")
        resolved = self._store.get_channel(channel_ref)
        if resolved is None:
            resolved = self._store.get_channel_by_name(channel_ref)
        if resolved is None:
            raise ValueError(f"Channel '{channel_ref}' not found")
        return resolved

    def _validate_subscription_scope(
        self,
        *,
        project_id: str | None,
        global_scope: bool,
        session_id: str | None,
    ) -> str | None:
        if global_scope:
            if project_id is not None:
                raise ValueError("Choose either project scope or global scope")
            if session_id is not None:
                raise ValueError("Global subscriptions cannot be session-scoped")
            return None

        if project_id is None or not project_id.strip():
            raise ValueError("Project scope or explicit global scope is required")
        if session_id is not None:
            session = self._session_store.get(session_id)
            if session is None:
                raise ValueError(f"Session '{session_id}' not found")
            if session.project_id != project_id:
                raise ValueError("Session does not belong to the selected project")
        return project_id

    def get_session_project_id(self, session_id: str) -> str | None:
        """Return the project for a session visible to communications."""
        session = self._session_store.get(session_id)
        return session.project_id if session is not None else None

    def create_event_subscription(
        self,
        *,
        name: str,
        channel: str,
        event_pattern: str,
        project_id: str | None,
        global_scope: bool,
        session_id: str | None = None,
        priority: int = 0,
        enabled: bool = True,
    ) -> CommsRoutingRule:
        """Create a validated event subscription."""
        normalized_name = name.strip()
        normalized_pattern = event_pattern.strip()
        if not normalized_name:
            raise ValueError("Subscription name is required")
        if not normalized_pattern:
            raise ValueError("Event pattern is required")

        resolved_channel = self._resolve_subscription_channel(channel)
        resolved_project_id = self._validate_subscription_scope(
            project_id=project_id,
            global_scope=global_scope,
            session_id=session_id,
        )
        now = utc_now()
        rule = CommsRoutingRule(
            id=str(uuid.uuid4()),
            name=normalized_name,
            channel_id=resolved_channel.id,
            event_pattern=normalized_pattern,
            project_id=resolved_project_id,
            session_id=session_id,
            priority=priority,
            enabled=enabled,
            config_json={},
            created_at=now,
            updated_at=now,
        )
        result = self._store.create_routing_rule(rule)
        self._router.invalidate_cache()
        return result

    def get_event_subscription(self, subscription_id: str) -> CommsRoutingRule:
        """Get an event subscription by ID."""
        rule = self._store.get_routing_rule(subscription_id)
        if rule is None:
            raise EventSubscriptionNotFoundError(
                f"Event subscription '{subscription_id}' not found"
            )
        return rule

    def list_event_subscriptions(
        self,
        *,
        channel: str | None = None,
        project_id: str | None = None,
        global_scope: bool | None = None,
        enabled: bool | None = None,
        event_pattern: str | None = None,
    ) -> list[CommsRoutingRule]:
        """List event subscriptions, including disabled entries by default."""
        if global_scope is True and project_id is not None:
            raise ValueError("Choose either project scope or global scope")
        channel_id = None
        if channel is not None:
            channel_id = self._resolve_subscription_channel(channel).id
        return self._store.list_routing_rules(
            channel_id=channel_id,
            project_id=project_id,
            global_scope=global_scope,
            enabled=enabled,
            event_pattern=event_pattern,
        )

    def update_event_subscription(
        self,
        subscription_id: str,
        *,
        name: str | None = None,
        channel: str | None = None,
        event_pattern: str | None = None,
        project_id: str | None = None,
        global_scope: bool | None = None,
        session_id: str | None | _Unset = _UNSET,
        priority: int | None = None,
        enabled: bool | None = None,
    ) -> CommsRoutingRule:
        """Partially update an event subscription."""
        current = self.get_event_subscription(subscription_id)
        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Subscription name is required")
            current.name = normalized_name
        if event_pattern is not None:
            normalized_pattern = event_pattern.strip()
            if not normalized_pattern:
                raise ValueError("Event pattern is required")
            current.event_pattern = normalized_pattern
        if channel is not None:
            current.channel_id = self._resolve_subscription_channel(channel).id

        next_session_id = current.session_id if isinstance(session_id, _Unset) else session_id
        next_project_id = current.project_id
        if global_scope is True:
            if project_id is not None:
                raise ValueError("Choose either project scope or global scope")
            next_project_id = None
        elif project_id is not None:
            next_project_id = project_id
        elif global_scope is False:
            raise ValueError("Project ID is required for project scope")
        current.project_id = self._validate_subscription_scope(
            project_id=next_project_id,
            global_scope=next_project_id is None,
            session_id=next_session_id,
        )
        current.session_id = next_session_id
        if priority is not None:
            current.priority = priority
        if enabled is not None:
            current.enabled = enabled
        current.updated_at = utc_now()
        current.config_json = dict(current.config_json)
        result = self._store.update_routing_rule(current)
        self._router.invalidate_cache()
        return result

    def delete_event_subscription(self, subscription_id: str) -> None:
        """Delete an event subscription by ID."""
        self.get_event_subscription(subscription_id)
        self._store.delete_routing_rule(subscription_id)
        self._router.invalidate_cache()

    def event_subscription_to_dict(self, rule: CommsRoutingRule) -> dict[str, Any]:
        """Serialize the public event-subscription contract."""
        if rule.channel_id is None:
            raise ValueError("Event subscription has no channel")
        channel = self._store.get_channel(rule.channel_id)
        if channel is None:
            raise ValueError(f"Channel '{rule.channel_id}' not found")

        def serialize_timestamp(value: object) -> str | None:
            return value.isoformat() if hasattr(value, "isoformat") else None

        return {
            "id": rule.id,
            "name": rule.name,
            "channel_id": channel.id,
            "channel_name": channel.name,
            "scope": {
                "kind": "global" if rule.project_id is None else "project",
                "project_id": rule.project_id,
            },
            "event_pattern": rule.event_pattern,
            "session_id": rule.session_id,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "created_at": serialize_timestamp(rule.created_at),
            "updated_at": serialize_timestamp(rule.updated_at),
        }

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
