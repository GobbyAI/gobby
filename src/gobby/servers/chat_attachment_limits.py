"""Chat attachment limit resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from gobby.storage.config_repository import ConfigRepositoryError

DEFAULT_ATTACHMENT_MAX_FILE_BYTES = 100_000_000
DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE = 20
DEFAULT_ATTACHMENT_MAX_TOTAL_BYTES_PER_MESSAGE = (
    DEFAULT_ATTACHMENT_MAX_FILE_BYTES * DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatAttachmentLimits:
    max_file_bytes: int = DEFAULT_ATTACHMENT_MAX_FILE_BYTES
    max_total_bytes_per_message: int = DEFAULT_ATTACHMENT_MAX_TOTAL_BYTES_PER_MESSAGE
    max_files_per_message: int = DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE

    def __post_init__(self) -> None:
        for field_name, value in (
            ("max_file_bytes", self.max_file_bytes),
            ("max_total_bytes_per_message", self.max_total_bytes_per_message),
            ("max_files_per_message", self.max_files_per_message),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        product_cap = self.max_file_bytes * self.max_files_per_message
        requested_total = self.max_total_bytes_per_message
        if requested_total > product_cap:
            is_builtin_default_cap = (
                requested_total == DEFAULT_ATTACHMENT_MAX_TOTAL_BYTES_PER_MESSAGE
                and self.max_file_bytes == DEFAULT_ATTACHMENT_MAX_FILE_BYTES
                and self.max_files_per_message == DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE
            )
            if not is_builtin_default_cap:
                logger.warning(
                    "Clamping chat attachment total byte limit from %s to %s "
                    "(max_file_bytes=%s, max_files_per_message=%s)",
                    requested_total,
                    product_cap,
                    self.max_file_bytes,
                    self.max_files_per_message,
                )
        object.__setattr__(
            self,
            "max_total_bytes_per_message",
            min(requested_total, product_cap),
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
    daemon_config: Any | None = None,
) -> ChatAttachmentLimits:
    """Resolve chat attachment limits from one typed runtime snapshot."""
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

    return ChatAttachmentLimits(
        max_file_bytes=default_max_file,
        max_total_bytes_per_message=default_max_total,
        max_files_per_message=default_max_count,
    )


def resolve_server_attachment_limits(server: Any) -> ChatAttachmentLimits:
    """Resolve attachment limits from a server's current runtime epoch."""
    services = getattr(server, "services", None)
    runtime = getattr(services, "config_runtime", None)
    capture = getattr(runtime, "capture", None)
    if runtime is not None and getattr(runtime, "ready", False) and callable(capture):
        return resolve_chat_attachment_limits(daemon_config=capture().snapshot.active)
    daemon_config = getattr(server, "config", None)
    store = getattr(services, "config_store", None)
    if store is not None and callable(getattr(store, "read_snapshot", None)):
        return ChatAttachmentLimits(
            max_file_bytes=_store_limit(
                store,
                "chat.attachment_max_file_bytes",
                _config_default(
                    daemon_config,
                    "attachment_max_file_bytes",
                    DEFAULT_ATTACHMENT_MAX_FILE_BYTES,
                ),
            ),
            max_total_bytes_per_message=_store_limit(
                store,
                "chat.attachment_max_total_bytes_per_message",
                _config_default(
                    daemon_config,
                    "attachment_max_total_bytes_per_message",
                    DEFAULT_ATTACHMENT_MAX_TOTAL_BYTES_PER_MESSAGE,
                ),
            ),
            max_files_per_message=_store_limit(
                store,
                "chat.attachment_max_files_per_message",
                _config_default(
                    daemon_config,
                    "attachment_max_files_per_message",
                    DEFAULT_ATTACHMENT_MAX_FILES_PER_MESSAGE,
                ),
            ),
        )
    return resolve_chat_attachment_limits(daemon_config=daemon_config)


def _store_limit(store: Any, key: str, fallback: int) -> int:
    try:
        value = store.read_snapshot().values.get(key)
    except (ConfigRepositoryError, ValueError, psycopg.Error):
        logger.warning("Config store read failed for %s; using fallback", key)
        return fallback
    return _positive_int(value, fallback)
