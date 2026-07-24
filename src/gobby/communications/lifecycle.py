"""Adapter lifecycle operations for communications."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.inbound_mode import channel_for_adapter_init, should_poll
from gobby.communications.models import ChannelConfig

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager

logger = logging.getLogger(__name__)


def _integration_secret_name(channel_type: str, key: str, channel_name: str) -> str:
    parts = (channel_type, key, channel_name)
    normalized = (re.sub(r"[^A-Za-z0-9_]", "_", part).upper() for part in parts)
    return "COMMS_" + "_".join(normalized)


_DEFAULT_CHANNEL_SECRET_REFS = {
    "discord": ("$secret:DISCORD_BOT_TOKEN",),
    "email": ("$secret:EMAIL_PASSWORD",),
}


def _get_adapter_class(channel_type: str) -> type[BaseChannelAdapter] | None:
    from gobby.communications.manager import _lookup_adapter_class

    return _lookup_adapter_class(channel_type)


class AdapterLifecycleOperations:
    """Coordinates adapter startup, shutdown, channel mutation, and status."""

    def __init__(self, manager: CommunicationsManager) -> None:
        self._manager = manager

    async def start(self) -> None:
        """Load enabled channels from DB, initialize adapters, configure rate limiter."""
        manager = self._manager
        await self._migrate_plaintext_webhook_secrets()
        await self.ensure_gobby_chat_channel()

        channels = await asyncio.to_thread(manager._store.list_channels, enabled_only=True)
        for channel in channels:
            try:
                await self.activate_channel(channel)
                logger.info(
                    "Communications: initialized channel %r (%s)",
                    channel.name,
                    channel.channel_type,
                )
            except Exception as e:
                logger.exception("Failed to initialize channel %r: %s", channel.name, e)
        logger.info("CommunicationsManager started (%s channels active)", len(manager._adapters))

    async def _migrate_plaintext_webhook_secrets(self) -> None:
        """Move legacy plaintext webhook secrets into SecretStore before activation."""
        manager = self._manager
        channels = await asyncio.to_thread(manager._store.list_channels, enabled_only=False)
        migrated = 0
        for channel in channels:
            secret = channel.webhook_secret
            if not secret or secret.startswith("$secret:"):
                continue

            secret_name = _integration_secret_name(
                channel.channel_type, "webhook_secret", channel.name
            )
            await asyncio.to_thread(
                manager._secret_store.set,
                name=secret_name,
                plaintext_value=secret,
                category="integration",
                description=f"{channel.channel_type} channel '{channel.name}': webhook_secret",
            )
            channel.webhook_secret = f"$secret:{secret_name}"
            channel.updated_at = datetime.now(UTC)
            await asyncio.to_thread(manager._store.update_channel, channel)
            migrated += 1

        if migrated:
            logger.info("Migrated %d plaintext communications webhook secret(s)", migrated)

    async def stop(self) -> None:
        """Shutdown all adapters and clear state."""
        manager = self._manager
        manager._polling_manager.stop_all()
        for name, adapter in list(manager._adapters.items()):
            try:
                await adapter.shutdown()
            except Exception as e:
                logger.exception("Error shutting down channel %r: %s", name, e)
        manager._adapters.clear()
        manager._channel_by_name.clear()
        manager._channel_init_errors.clear()
        logger.info("CommunicationsManager stopped")

    async def init_adapter(self, channel: ChannelConfig) -> BaseChannelAdapter:
        """Lookup adapter class from registry, instantiate, and initialize."""
        manager = self._manager
        adapter_cls = _get_adapter_class(channel.channel_type)
        if adapter_cls is None:
            raise ValueError(f"No adapter registered for channel type {channel.channel_type!r}")
        adapter = adapter_cls()

        def on_rate_limit(duration: float, is_global: bool = False, cid: str = channel.id) -> None:
            if is_global:
                for existing_channel in manager._channel_by_name.values():
                    if existing_channel.channel_type == channel.channel_type:
                        manager._rate_limiter.set_backoff(existing_channel.id, duration)
                logger.warning(
                    "Global rate limit hit on %s, backing off ALL %s channels for %.1fs",
                    channel.name,
                    channel.channel_type,
                    duration,
                )
            else:
                manager._rate_limiter.set_backoff(cid, duration)

        adapter.set_rate_limit_callback(on_rate_limit)
        adapter.set_inbound_callback(
            lambda msgs: manager.handle_inbound_messages(channel.name, msgs)
        )

        async def update_config(values: dict[str, Any]) -> None:
            channel.config_json.update(values)
            channel.updated_at = datetime.now(UTC)
            await asyncio.to_thread(manager._store.update_channel, channel)

        adapter.set_config_update_callback(update_config)

        secret_refs: set[str] = set()
        for value in channel.config_json.values():
            if isinstance(value, str) and value.startswith("$secret:"):
                secret_refs.add(value)
        if channel.webhook_secret and channel.webhook_secret.startswith("$secret:"):
            secret_refs.add(channel.webhook_secret)
        secret_refs.update(_DEFAULT_CHANNEL_SECRET_REFS.get(channel.channel_type, ()))
        resolved_secret_refs: dict[str, str | None] = {}
        for ref in secret_refs:
            name = ref.removeprefix("$secret:")
            resolved = await asyncio.to_thread(manager._secret_store.get, name)
            resolved_secret_refs[ref] = resolved
            resolved_secret_refs[name] = resolved

        def resolve_secret_ref(ref: str) -> str | None:
            return resolved_secret_refs.get(ref)

        init_channel = channel_for_adapter_init(channel, adapter, manager._config.webhook_base_url)
        if init_channel.webhook_secret and init_channel.webhook_secret.startswith("$secret:"):
            resolved_webhook_secret = resolve_secret_ref(init_channel.webhook_secret)
            if resolved_webhook_secret is None:
                raise ValueError(f"Webhook secret for channel {channel.name!r} is not configured")
            init_channel = replace(init_channel, webhook_secret=resolved_webhook_secret)
        await adapter.initialize(init_channel, resolve_secret_ref)
        return adapter

    async def activate_channel(self, channel: ChannelConfig) -> BaseChannelAdapter:
        """Initialize a channel adapter and publish active runtime state."""
        manager = self._manager
        self._configure_rate_limit(channel, channel.config_json)
        adapter = await self.init_adapter(channel)
        manager._adapters[channel.name] = adapter
        manager._channel_by_name[channel.name] = channel
        if channel.name == "gobby_chat" and manager._websocket_broadcast is not None:
            self.set_websocket_broadcast(manager._websocket_broadcast)
        manager._channel_init_errors.pop(channel.name, None)
        self._start_polling_if_needed(channel.name, channel, adapter)
        return adapter

    async def deactivate_channel(self, name: str, channel: ChannelConfig | None = None) -> None:
        """Shutdown an active adapter and clear runtime state."""
        manager = self._manager
        adapter = manager._adapters.pop(name, None)
        cached_channel = manager._channel_by_name.pop(name, None)
        manager._channel_init_errors.pop(name, None)
        manager._polling_manager.stop_polling(name)

        channel = channel or cached_channel
        if channel is not None:
            manager._rate_limiter.remove_channel(channel.id)

        if adapter is None:
            return

        try:
            await adapter.shutdown()
        except Exception as e:
            logger.exception("Error shutting down channel %r: %s", name, e)

    async def add_channel(
        self,
        channel_type: str,
        name: str,
        config: dict[str, Any],
        secrets: dict[str, Any] | None = None,
    ) -> ChannelConfig:
        """Create a new channel in DB and initialize its adapter."""
        manager = self._manager
        now = datetime.now(UTC)
        webhook_secret: str | None = None
        config_json = dict(config)

        if secrets:
            for key, value in secrets.items():
                if not value:
                    continue
                secret_name = _integration_secret_name(channel_type, key, name)
                await asyncio.to_thread(
                    manager._secret_store.set,
                    name=secret_name,
                    plaintext_value=str(value),
                    category="integration",
                    description=f"{channel_type} channel '{name}': {key}",
                )
                secret_ref = f"$secret:{secret_name}"
                if key == "webhook_secret":
                    webhook_secret = secret_ref
                else:
                    config_json[key] = secret_ref
        channel_config = ChannelConfig(
            id=str(uuid.uuid4()),
            channel_type=channel_type,
            name=name,
            enabled=True,
            config_json=config_json,
            created_at=now,
            updated_at=now,
            webhook_secret=webhook_secret,
        )

        await asyncio.to_thread(manager._store.create_channel, channel_config)

        try:
            await self.activate_channel(channel_config)
            logger.info("Added channel %r (%s)", name, channel_type)
        except Exception as e:
            manager._channel_init_errors[name] = str(e)
            logger.error("Failed to initialize adapter for new channel %r: %s", name, e)

        return channel_config

    async def remove_channel(self, name: str) -> None:
        """Shutdown adapter and delete channel from DB."""
        manager = self._manager
        channel = manager._channel_by_name.get(name) or await asyncio.to_thread(
            manager._store.get_channel_by_name, name
        )
        if channel is None:
            raise ValueError(f"Channel {name!r} not found")

        await self.deactivate_channel(name, channel)

        try:
            await asyncio.to_thread(manager._store.delete_channel, channel.id)
            logger.info("Removed channel %r", name)
        except Exception as e:
            logger.error("Failed to delete channel %r from DB: %s", name, e)
            raise

    async def ensure_gobby_chat_channel(self) -> None:
        """Auto-create a gobby_chat channel if one doesn't already exist."""
        manager = self._manager
        channels = await asyncio.to_thread(manager._store.list_channels, enabled_only=False)
        if any(channel.channel_type == "gobby_chat" for channel in channels):
            return

        if _get_adapter_class("gobby_chat") is None:
            logger.debug("gobby_chat adapter not registered, skipping auto-create")
            return

        now = datetime.now(UTC)
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
            await asyncio.to_thread(manager._store.create_channel, channel)
            logger.info("Auto-created gobby_chat channel for unified routing")
        except Exception as e:
            logger.exception("Failed to auto-create gobby_chat channel: %s", e)

    def set_websocket_broadcast(self, broadcast: Any) -> None:
        """Wire the WebSocket broadcast callable into the gobby_chat adapter."""
        from gobby.communications.adapters.gobby_chat import GobbyChatAdapter

        adapter = self._manager._adapters.get("gobby_chat")
        if isinstance(adapter, GobbyChatAdapter):
            adapter.set_broadcast(broadcast)
            logger.info("GobbyChatAdapter wired to WebSocket broadcast")

    async def update_channel(
        self, channel: ChannelConfig, secrets: dict[str, Any] | None = None
    ) -> ChannelConfig:
        """Update channel config and reconcile runtime adapter state."""
        manager = self._manager
        if secrets:
            for key, value in secrets.items():
                if not value:
                    continue
                secret_name = _integration_secret_name(channel.channel_type, key, channel.name)
                await asyncio.to_thread(
                    manager._secret_store.set,
                    name=secret_name,
                    plaintext_value=str(value),
                    category="integration",
                    description=f"{channel.channel_type} channel '{channel.name}': {key}",
                )
                secret_ref = f"$secret:{secret_name}"
                if key == "webhook_secret":
                    channel.webhook_secret = secret_ref
                else:
                    channel.config_json[key] = secret_ref

        channel.updated_at = datetime.now(UTC)
        updated = await asyncio.to_thread(manager._store.update_channel, channel)

        current_names = [
            name for name, cached in manager._channel_by_name.items() if cached.id == updated.id
        ]
        if updated.name not in current_names:
            current_names.append(updated.name)

        for name in current_names:
            await self.deactivate_channel(name, updated)

        if not updated.enabled:
            return updated

        try:
            await self.activate_channel(updated)
        except Exception as e:
            manager._channel_init_errors[updated.name] = str(e)
            logger.error("Failed to initialize adapter for updated channel %r: %s", updated.name, e)

        return updated

    def get_channel_status(self, name: str) -> dict[str, Any]:
        """Get adapter health/connected status for a channel."""
        manager = self._manager
        channel = manager._channel_by_name.get(name)
        adapter = manager._adapters.get(name)

        if channel is not None and adapter is not None:
            return {
                "name": name,
                "channel_type": channel.channel_type,
                "status": "active",
                "active": True,
                "enabled": channel.enabled,
                "supports_webhooks": adapter.supports_webhooks,
                "supports_polling": adapter.supports_polling,
                "is_polling": manager._polling_manager.is_polling(name),
                "init_error": None,
            }

        channels = manager._store.list_channels(enabled_only=False)
        db_channel = next((candidate for candidate in channels if candidate.name == name), None)
        if db_channel is None:
            return {"name": name, "status": "not_found", "active": False}

        return {
            "name": name,
            "channel_type": db_channel.channel_type,
            "status": "inactive",
            "active": False,
            "enabled": db_channel.enabled,
            "init_error": manager._channel_init_errors.get(name),
        }

    def _configure_rate_limit(self, channel: ChannelConfig, config: dict[str, Any]) -> None:
        manager = self._manager
        rate = config.get(
            "rate_limit_per_minute",
            manager._config.channel_defaults.rate_limit_per_minute,
        )
        burst = config.get(
            "burst",
            manager._config.channel_defaults.burst,
        )
        manager._rate_limiter.configure_channel(channel.id, int(rate), int(burst))

    def _start_polling_if_needed(
        self, channel_name: str, channel: ChannelConfig, adapter: BaseChannelAdapter
    ) -> None:
        manager = self._manager
        if should_poll(adapter, manager._config.webhook_base_url):
            interval = channel.config_json.get("poll_interval")
            manager._polling_manager.start_polling(channel_name, adapter, interval)
