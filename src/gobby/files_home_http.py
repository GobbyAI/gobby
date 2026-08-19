"""Shared hub-files HTTP contract: hop bound, USER.md limits, owner URL."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FILES_PROXY_HOP_HEADER = "X-Gobby-Files-Proxy-Hop"
USER_MD_PATH = "/api/files/user-md"
USER_MD_CONTENT_MAX_BYTES = 1_048_576
USER_MD_WIRE_MAX_BYTES = 6_291_470
CONNECT_TIMEOUT_SECONDS = 3.0
INACTIVITY_TIMEOUT_SECONDS = 30.0
DEADLINE_TIMEOUT_SECONDS = 120.0
PROXY_ACCEPT_STATUSES = (200, 206, 304)
FORWARD_REQUEST_HEADERS = ("range", "if-none-match", "if-modified-since")
FORWARD_RESPONSE_HEADERS = (
    "content-type",
    "content-disposition",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
)


class FilesProxyError(RuntimeError):
    """Typed files-proxy failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def hop_header_present(headers: Mapping[str, Any]) -> bool:
    value = headers.get(FILES_PROXY_HOP_HEADER)
    if value is None:
        value = headers.get(FILES_PROXY_HOP_HEADER.lower())
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(str(item).strip() for item in value)
    return bool(str(value).strip())


def is_remote_files_mode() -> bool:
    from gobby.config.bootstrap import load_bootstrap

    return load_bootstrap().datastore_mode == "remote"


def get_hub_daemon_url() -> str | None:
    from gobby.config.bootstrap import load_bootstrap

    config = load_bootstrap()
    if config.datastore_mode != "remote":
        return None
    return config.hub_daemon_url


def require_hub_daemon_url() -> str:
    origin = get_hub_daemon_url()
    if not origin:
        raise FilesProxyError("hub_url_missing", "hub_daemon_url is required in remote mode")
    return origin
