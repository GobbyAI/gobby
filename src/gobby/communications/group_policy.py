"""Shared group-message access and wake policy evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from gobby.communications.models import CommsMessage

_DIRECT_CONVERSATION_TYPES = frozenset({"direct", "dm", "im", "private"})
_GROUP_CONVERSATION_TYPES = frozenset({"channel", "group", "mpim", "room", "supergroup"})


@dataclass(frozen=True, slots=True)
class GroupMessageDecision:
    """Resolved access and wake behavior for one communications message."""

    sender_id: str | None
    conversation_id: str
    is_group: bool
    group_config: dict[str, object]
    authorized: bool
    should_respond: bool
    reason: str


def evaluate_group_message(
    channel_config: Mapping[str, object],
    message: CommsMessage,
) -> GroupMessageDecision:
    """Resolve group authorization and whether this message should wake the responder."""
    sender_id = _sender_id(message)
    conversation_id = _conversation_id(message)
    is_group = _is_group_message(message)
    if not is_group:
        return GroupMessageDecision(
            sender_id=sender_id,
            conversation_id=conversation_id,
            is_group=False,
            group_config={},
            authorized=True,
            should_respond=True,
            reason="not_group",
        )

    groups = _object_mapping(channel_config.get("groups"))
    group_is_configured = conversation_id in groups or "*" in groups
    group_config = {
        **_object_mapping(groups.get("*")),
        **_object_mapping(groups.get(conversation_id)),
    }
    policy_value = group_config.get(
        "group_policy",
        channel_config.get("group_policy", "allowlist"),
    )
    policy = policy_value.casefold() if isinstance(policy_value, str) else "disabled"
    if policy not in {"allowlist", "open"}:
        return _group_denial(
            sender_id,
            conversation_id,
            group_config,
            f"group_policy_{policy}",
        )
    if groups and not group_is_configured:
        return _group_denial(
            sender_id,
            conversation_id,
            group_config,
            "group_not_configured",
        )
    if policy == "allowlist" and not group_is_configured:
        return _group_denial(
            sender_id,
            conversation_id,
            group_config,
            "group_not_allowlisted",
        )

    allow_from_value = (
        group_config["allow_from"]
        if "allow_from" in group_config
        else channel_config.get("allow_from")
    )
    allow_from = _string_set(allow_from_value)
    if policy == "allowlist" and (
        sender_id is None or (sender_id not in allow_from and "*" not in allow_from)
    ):
        return _group_denial(
            sender_id,
            conversation_id,
            group_config,
            "sender_not_allowlisted",
        )

    require_mention_value = group_config.get(
        "require_mention",
        channel_config.get("require_mention", True),
    )
    require_mention = require_mention_value if isinstance(require_mention_value, bool) else True
    should_respond = not require_mention or _is_mentioned(message)
    return GroupMessageDecision(
        sender_id=sender_id,
        conversation_id=conversation_id,
        is_group=True,
        group_config=group_config,
        authorized=True,
        should_respond=should_respond,
        reason="wake" if should_respond else "mention_required",
    )


def _group_denial(
    sender_id: str | None,
    conversation_id: str,
    group_config: dict[str, object],
    reason: str,
) -> GroupMessageDecision:
    return GroupMessageDecision(
        sender_id=sender_id,
        conversation_id=conversation_id,
        is_group=True,
        group_config=group_config,
        authorized=False,
        should_respond=False,
        reason=reason,
    )


def _object_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item) for item in value if not isinstance(item, bool) and isinstance(item, str | int)
    }


def _sender_id(message: CommsMessage) -> str | None:
    raw_sender = message.metadata_json.get("external_user_id", message.identity_id)
    if not isinstance(raw_sender, bool) and isinstance(raw_sender, str | int):
        return str(raw_sender)
    return None


def _conversation_id(message: CommsMessage) -> str:
    for key in ("platform_channel_id", "chat_id"):
        value = message.metadata_json.get(key)
        if not isinstance(value, bool) and isinstance(value, str | int) and str(value):
            return str(value)
    conversation_reference = _object_mapping(message.metadata_json.get("conversation_reference"))
    reference_id = conversation_reference.get("conversation_id")
    if (
        not isinstance(reference_id, bool)
        and isinstance(reference_id, str | int)
        and str(reference_id)
    ):
        return str(reference_id)
    return message.session_id or message.id


def _is_group_message(message: CommsMessage) -> bool:
    raw_is_group = message.metadata_json.get("is_group")
    if isinstance(raw_is_group, bool):
        return raw_is_group
    for key in ("conversation_type", "chat_type", "channel_type"):
        value = message.metadata_json.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.casefold()
        if normalized in _GROUP_CONVERSATION_TYPES:
            return True
        if normalized in _DIRECT_CONVERSATION_TYPES:
            return False
    return False


def _is_mentioned(message: CommsMessage) -> bool:
    for key in ("mentioned", "is_mentioned"):
        value = message.metadata_json.get(key)
        if isinstance(value, bool):
            return value
    return False
