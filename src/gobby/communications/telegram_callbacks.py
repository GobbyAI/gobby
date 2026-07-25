"""Bounded Telegram inline keyboards and callback-query normalization."""

from __future__ import annotations

import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.communications.models import CommsMessage

_CALLBACK_PREFIX = "gobby:"
_MAX_ROWS = 8
_MAX_BUTTONS_PER_ROW = 8
_MAX_TOTAL_BUTTONS = 32
_MAX_BUTTON_TEXT_LENGTH = 64
_MAX_BUTTON_VALUE_BYTES = 1024
_MIN_TTL_SECONDS = 1
_MAX_TTL_SECONDS = 3600
_DEFAULT_MAX_ENTRIES = 2048


@dataclass(frozen=True)
class TelegramCallbackResolution:
    """Outcome of resolving one opaque Telegram callback token."""

    status: Literal["ok", "expired", "invalid"]
    session_id: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class _CallbackEntry:
    session_id: str
    value: str
    chat_id: str
    thread_id: str | None
    expires_at: float


class TelegramCallbackRegistry:
    """Create opaque callback tokens and resolve them once within a bounded TTL."""

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("Telegram callback max_entries must be positive")
        self._max_entries = max_entries
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(18))
        self._entries: OrderedDict[str, _CallbackEntry] = OrderedDict()

    def register_keyboard(
        self,
        keyboard: object,
        *,
        session_id: str | None,
        chat_id: str,
        thread_id: str | None,
        ttl_seconds: object,
    ) -> dict[str, list[list[dict[str, str]]]]:
        """Validate a keyboard and replace button values with opaque callback tokens."""
        normalized_session_id = _required_string(session_id, "session_id")
        normalized_chat_id = _required_string(chat_id, "chat_id")
        normalized_thread_id = _optional_string(thread_id)
        ttl = _bounded_ttl(ttl_seconds)
        buttons = _normalized_keyboard(keyboard)
        button_count = sum(len(row) for row in buttons)
        if button_count > self._max_entries:
            raise ValueError("Telegram inline_keyboard exceeds the callback registry capacity")
        self._prune_expired()

        expires_at = self._clock() + ttl
        telegram_rows: list[list[dict[str, str]]] = []
        for row in buttons:
            telegram_row: list[dict[str, str]] = []
            for text, value in row:
                token = self._new_token()
                callback_data = f"{_CALLBACK_PREFIX}{token}"
                if len(callback_data.encode("utf-8")) > 64:
                    raise ValueError("Telegram callback token exceeds the 64-byte platform limit")
                self._entries[token] = _CallbackEntry(
                    session_id=normalized_session_id,
                    value=value,
                    chat_id=normalized_chat_id,
                    thread_id=normalized_thread_id,
                    expires_at=expires_at,
                )
                self._evict_excess()
                telegram_row.append({"text": text, "callback_data": callback_data})
            telegram_rows.append(telegram_row)
        return {"inline_keyboard": telegram_rows}

    def resolve(
        self,
        callback_data: object,
        *,
        chat_id: str,
        thread_id: str | None,
    ) -> TelegramCallbackResolution:
        """Authenticate, scope, and consume one callback token."""
        if not isinstance(callback_data, str) or not callback_data.startswith(_CALLBACK_PREFIX):
            return TelegramCallbackResolution(status="invalid")
        token = callback_data.removeprefix(_CALLBACK_PREFIX)
        if not token:
            return TelegramCallbackResolution(status="invalid")
        entry = self._entries.get(token)
        if entry is None:
            return TelegramCallbackResolution(status="invalid")
        if entry.expires_at <= self._clock():
            self._entries.pop(token, None)
            return TelegramCallbackResolution(status="expired")
        if entry.chat_id != str(chat_id) or entry.thread_id != _optional_string(thread_id):
            return TelegramCallbackResolution(status="invalid")

        self._entries.pop(token, None)
        return TelegramCallbackResolution(
            status="ok",
            session_id=entry.session_id,
            value=entry.value,
        )

    def _new_token(self) -> str:
        for _attempt in range(10):
            token = self._token_factory()
            if token and token not in self._entries:
                return token
        raise RuntimeError("Could not allocate a unique Telegram callback token")

    def _prune_expired(self) -> None:
        now = self._clock()
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            self._entries.pop(token, None)

    def _evict_excess(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


def telegram_callback_message(
    payload: Mapping[str, Any],
    registry: TelegramCallbackRegistry,
) -> CommsMessage | None:
    """Normalize a Telegram callback query into a session-routable inbound message."""
    query = payload.get("callback_query")
    if not isinstance(query, Mapping):
        return None
    callback_id = query.get("id")
    callback_data = query.get("data")
    from_user = query.get("from")
    source_message = query.get("message")
    if (
        not isinstance(callback_id, str)
        or not callback_id
        or not isinstance(from_user, Mapping)
        or not isinstance(source_message, Mapping)
    ):
        return None

    chat = source_message.get("chat")
    if not isinstance(chat, Mapping):
        return None
    chat_id = _platform_identifier(chat.get("id"))
    user_id = _platform_identifier(from_user.get("id"))
    if chat_id is None or user_id is None:
        return None
    thread_id = _positive_integer_identifier(source_message.get("message_thread_id"))
    resolution = registry.resolve(
        callback_data,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    username = from_user.get("username")
    external_username = username if isinstance(username, str) and username else user_id
    conversation_type = chat.get("type")
    if not isinstance(conversation_type, str):
        conversation_type = "unknown"

    metadata: dict[str, Any] = {
        "chat_id": chat_id,
        "platform_channel_id": chat_id,
        "conversation_type": conversation_type,
        "mentioned": True,
        "conversation_reference": {"conversation_id": chat_id},
        "telegram_update_id": payload.get("update_id"),
        "telegram_callback_query_id": callback_id,
        "callback_status": resolution.status,
        "user_id": user_id,
        "username": username,
        "external_username": external_username,
    }
    if thread_id is not None:
        metadata["message_thread_id"] = thread_id
        metadata["is_topic_message"] = True
    if resolution.status == "ok":
        metadata["callback_session_id"] = resolution.session_id
        metadata["callback_value"] = resolution.value

    source_message_id = _platform_identifier(source_message.get("message_id"))
    if source_message_id is not None:
        metadata["callback_source_message_id"] = source_message_id
    return CommsMessage(
        id=str(uuid.uuid4()),
        channel_id="",
        direction="inbound",
        content=resolution.value or "",
        content_type="callback",
        platform_message_id=f"callback:{callback_id}",
        platform_thread_id=thread_id,
        session_id=resolution.session_id,
        identity_id=user_id,
        metadata_json=metadata,
        created_at=datetime.now(UTC),
    )


def _normalized_keyboard(keyboard: object) -> list[list[tuple[str, str]]]:
    if not isinstance(keyboard, list) or not keyboard:
        raise ValueError("Telegram inline_keyboard must contain at least one row")
    if len(keyboard) > _MAX_ROWS:
        raise ValueError(f"Telegram inline_keyboard supports at most {_MAX_ROWS} rows")

    normalized: list[list[tuple[str, str]]] = []
    total = 0
    for row in keyboard:
        if not isinstance(row, list) or not row:
            raise ValueError("Telegram inline_keyboard rows must be non-empty lists")
        if len(row) > _MAX_BUTTONS_PER_ROW:
            raise ValueError(
                f"Telegram inline_keyboard supports at most {_MAX_BUTTONS_PER_ROW} buttons per row"
            )
        normalized_row: list[tuple[str, str]] = []
        for button in row:
            if not isinstance(button, Mapping):
                raise ValueError("Telegram inline_keyboard buttons must be objects")
            text = _required_string(button.get("text"), "button text")
            value = _required_string(button.get("value"), "button value")
            if len(text) > _MAX_BUTTON_TEXT_LENGTH:
                raise ValueError(
                    f"Telegram button text supports at most {_MAX_BUTTON_TEXT_LENGTH} characters"
                )
            if len(value.encode("utf-8")) > _MAX_BUTTON_VALUE_BYTES:
                raise ValueError(
                    f"Telegram button values support at most {_MAX_BUTTON_VALUE_BYTES} bytes"
                )
            normalized_row.append((text, value))
        total += len(normalized_row)
        normalized.append(normalized_row)
    if total > _MAX_TOTAL_BUTTONS:
        raise ValueError(
            f"Telegram inline_keyboard supports at most {_MAX_TOTAL_BUTTONS} total buttons"
        )
    return normalized


def _bounded_ttl(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Telegram callback_ttl_seconds must be an integer")
    if not _MIN_TTL_SECONDS <= value <= _MAX_TTL_SECONDS:
        raise ValueError(
            "Telegram callback_ttl_seconds must be between "
            f"{_MIN_TTL_SECONDS} and {_MAX_TTL_SECONDS}"
        )
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Telegram callback {name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _platform_identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _positive_integer_identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return str(value)
