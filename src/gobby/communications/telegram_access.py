"""Telegram direct-message enrollment and access helpers."""

from __future__ import annotations

from collections.abc import Mapping

from gobby.communications.models import ChannelConfig, CommsMessage

_DIRECT_CONVERSATION_TYPES = frozenset({"direct", "dm", "im", "private"})


def is_telegram_dm(channel: ChannelConfig, message: CommsMessage) -> bool:
    """Return whether message belongs to a Telegram direct conversation."""
    return (
        channel.channel_type == "telegram"
        and message.metadata_json.get("conversation_type") in _DIRECT_CONVERSATION_TYPES
    )


def telegram_dm_sender(channel: ChannelConfig, message: CommsMessage) -> str | None:
    """Return the external sender for a Telegram private message."""
    if not is_telegram_dm(channel, message):
        return None
    raw_sender = message.identity_id
    if isinstance(raw_sender, bool) or not isinstance(raw_sender, str | int):
        return None
    sender = str(raw_sender).strip()
    return sender or None


def allowed_senders(config: Mapping[str, object]) -> set[str]:
    """Normalize the persisted direct-message allowlist."""
    value = config.get("allow_from")
    if not isinstance(value, list):
        return set()
    return {
        str(item) for item in value if not isinstance(item, bool) and isinstance(item, str | int)
    }


def is_deliberate_start(content: str) -> bool:
    """Return whether content is exactly Telegram's private enrollment command."""
    stripped = content.strip()
    if not stripped.startswith("/"):
        return False
    token, *arguments = stripped.split()
    command = token[1:].split("@", maxsplit=1)[0].casefold()
    return command == "start" and not arguments
