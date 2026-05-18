"""Chat attachment limit resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_ATTACHMENT_MAX_FILE_BYTES = 100_000_000
DEFAULT_ATTACHMENT_MAX_TOTAL_BYTES_PER_MESSAGE = 2_147_483_648
DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE = 20

_MAX_FILE_KEY = "chat.attachment_max_file_bytes"
_MAX_TOTAL_KEY = "chat.attachment_max_total_bytes_per_message"
_MAX_COUNT_KEY = "chat.attachment_max_files_per_message"


@dataclass(frozen=True)
class ChatAttachmentLimits:
    max_file_bytes: int = DEFAULT_ATTACHMENT_MAX_FILE_BYTES
    max_total_bytes_per_message: int = DEFAULT_ATTACHMENT_MAX_TOTAL_BYTES_PER_MESSAGE
    max_files_per_message: int = DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE

    def __post_init__(self) -> None:
        product_cap = self.max_file_bytes * self.max_files_per_message
        object.__setattr__(
            self,
            "max_total_bytes_per_message",
            min(self.max_total_bytes_per_message, product_cap),
        )


def _positive_int(value: Any, fallback: int) -> int:
    # bool is an int subclass; config booleans must not become 1/0 byte limits.
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int) and value > 0:
        return value
    return fallback


def _config_default(daemon_config: Any, attr: str, fallback: int) -> int:
    chat_config = getattr(daemon_config, "chat", None) if daemon_config is not None else None
    return _positive_int(getattr(chat_config, attr, None), fallback)


def resolve_chat_attachment_limits(
    *,
    config_store: Any | None = None,
    daemon_config: Any | None = None,
) -> ChatAttachmentLimits:
    """Resolve chat attachment limits from DB config_store, then daemon defaults."""
    default_max_file = _config_default(
        daemon_config,
        "attachment_max_file_bytes",
        DEFAULT_ATTACHMENT_MAX_FILE_BYTES,
    )
    default_max_count = _config_default(
        daemon_config,
        "attachment_max_files_per_message",
        DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE,
    )
    default_max_total = _config_default(
        daemon_config,
        "attachment_max_total_bytes_per_message",
        DEFAULT_ATTACHMENT_MAX_TOTAL_BYTES_PER_MESSAGE,
    )

    store_max_file = None
    store_max_total = None
    store_max_count = None
    if config_store is not None:
        store_max_file = config_store.get(_MAX_FILE_KEY)
        store_max_total = config_store.get(_MAX_TOTAL_KEY)
        store_max_count = config_store.get(_MAX_COUNT_KEY)

    return ChatAttachmentLimits(
        max_file_bytes=_positive_int(store_max_file, default_max_file),
        max_total_bytes_per_message=_positive_int(store_max_total, default_max_total),
        max_files_per_message=_positive_int(store_max_count, default_max_count),
    )
