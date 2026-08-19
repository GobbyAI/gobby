"""User profile seeding for session start."""

from __future__ import annotations

from typing import Any

from gobby.files_home_http import (
    FILES_PROXY_HOP_HEADER,
    USER_MD_CONTENT_MAX_BYTES,
    USER_MD_PATH,
    require_hub_daemon_url,
)
from gobby.paths import (
    FilesHomeNotOnThisDaemonError,
    publish_files_home_descendant,
    require_files_home,
)
from gobby.utils.daemon_client import DaemonClient
from gobby.workflows.state_manager import SessionVariableManager

USER_PROFILE_FILENAME = "USER.md"


class UserProfileError(RuntimeError):
    """Typed failure reading or writing USER.md."""


def _fetch_user_md() -> tuple[int, object]:
    client = DaemonClient.from_url(require_hub_daemon_url())
    response = client.call_http_api(
        USER_MD_PATH,
        method="GET",
        headers={FILES_PROXY_HOP_HEADER: "1"},
    )
    try:
        payload: object = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


def _read_remote_profile() -> str:
    status, payload = _fetch_user_md()
    if status != 200 or not isinstance(payload, dict):
        raise UserProfileError(f"hub USER.md read failed with HTTP {status}")
    content = payload.get("content")
    if not isinstance(content, str):
        raise UserProfileError("hub USER.md body must contain a string content field")
    return content


def _write_remote_profile(content: str) -> None:
    client = DaemonClient.from_url(require_hub_daemon_url())
    response = client.call_http_api(
        USER_MD_PATH,
        method="PUT",
        json_data={"content": content},
        headers={FILES_PROXY_HOP_HEADER: "1"},
    )
    if response.status_code != 200:
        raise UserProfileError(f"hub USER.md write failed with HTTP {response.status_code}")


def read_user_profile_content() -> str:
    """Read the hub-owner profile, or fetch it from hub_daemon_url on a node."""
    try:
        path = require_files_home() / USER_PROFILE_FILENAME
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
    except FilesHomeNotOnThisDaemonError:
        return _read_remote_profile()


def write_user_profile_content(content: str) -> None:
    """Write USER.md on the owner or PUT it to the hub. Never mkdir a node tree."""
    if len(content.encode("utf-8")) > USER_MD_CONTENT_MAX_BYTES:
        raise UserProfileError("USER.md content exceeds decoded limit")
    try:
        require_files_home()
        publish_files_home_descendant(USER_PROFILE_FILENAME, content.encode("utf-8"))
    except FilesHomeNotOnThisDaemonError:
        _write_remote_profile(content)


def seed_user_profile_content(handler: Any, session_id: str | None) -> None:
    """Persist the global user profile content into session variables."""
    session_manager = handler.get_session_manager()
    if not session_id or session_manager is None:
        return

    try:
        content = read_user_profile_content()
    except UserProfileError as exc:
        handler.logger.warning("Failed to read global user profile: %s", exc)
        content = ""
    except OSError as exc:
        handler.logger.warning("Failed to read global user profile: %s", exc)
        content = ""

    SessionVariableManager(session_manager.db).merge_variables(
        session_id,
        {"user_profile_content": content},
    )
