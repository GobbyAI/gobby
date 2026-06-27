"""ACP session state and capability tracking helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

DEFAULT_ACP_CLIENT_CAPABILITIES: Mapping[str, Any] = MappingProxyType(
    {
        "terminal": True,
        "fs": {
            "readTextFile": True,
            "writeTextFile": True,
        },
    }
)


def copy_default_acp_client_capabilities() -> dict[str, Any]:
    return {
        "terminal": True,
        "fs": {
            "readTextFile": True,
            "writeTextFile": True,
        },
    }


def extract_session_id(payload: Any) -> str | None:
    """Extract an ACP session ID from common response/notification shapes."""
    if not isinstance(payload, dict):
        return None

    for key in ("sessionId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    session = payload.get("session")
    if isinstance(session, dict):
        nested = extract_session_id(session)
        if nested:
            return nested

    result = payload.get("result")
    if isinstance(result, dict):
        nested = extract_session_id(result)
        if nested:
            return nested

    return None


def extract_root_uris(payload: Any) -> tuple[str, ...]:
    """Extract ACP root/workspace URI values from known response shapes."""
    if not isinstance(payload, dict):
        return ()

    roots = payload.get("roots")
    if roots is None:
        roots = payload.get("workspaceRoots")
    if not isinstance(roots, list):
        return ()

    root_uris: list[str] = []
    for root in roots:
        if isinstance(root, str) and root:
            root_uris.append(root)
        elif isinstance(root, dict):
            uri = root.get("uri") or root.get("path")
            if isinstance(uri, str) and uri:
                root_uris.append(uri)
    return tuple(root_uris)


# ACP wire (camelCase) -> internal snake_case capability key. Camel-case names
# are translated here so ACP protocol vocabulary never leaks past this seam.
_SESSION_CAPABILITY_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "list": "list",
        "resume": "resume",
        "close": "close",
        "delete": "delete",
        "additionalDirectories": "additional_directories",
    }
)


def parse_session_capabilities(capabilities: Any) -> dict[str, bool]:
    """Parse ``agentCapabilities.sessionCapabilities`` with presence-not-null semantics.

    The ACP wire shape is an object of optional sub-objects keyed
    ``list``/``resume``/``close``/``delete``/``additionalDirectories``. A key
    present with a non-null value (e.g. ``{}``) means the capability is
    supported; an omitted or ``null`` value means unsupported. A boolean parse
    would invert this. Camel-case wire keys map to snake_case internal keys.
    """
    parsed = dict.fromkeys(_SESSION_CAPABILITY_KEYS.values(), False)
    raw = capabilities.get("sessionCapabilities") if isinstance(capabilities, dict) else None
    if not isinstance(raw, dict):
        return parsed
    for wire_key, internal_key in _SESSION_CAPABILITY_KEYS.items():
        if raw.get(wire_key) is not None:
            parsed[internal_key] = True
    return parsed


def extract_session_infos(payload: Any) -> list[dict[str, Any]]:
    """Extract ACP ``SessionInfo`` entries from a ``session/list`` result.

    Accepts the full result object (``{"sessions": [...]}``) or a bare list of
    ``SessionInfo`` objects. Non-dict entries are dropped. Pagination cursors
    are handled by callers, not here.
    """
    if isinstance(payload, dict):
        sessions: Any = payload.get("sessions")
    elif isinstance(payload, list):
        sessions = payload
    else:
        sessions = None
    if not isinstance(sessions, list):
        return []
    return [dict(item) for item in sessions if isinstance(item, dict)]


@dataclass
class ACPSessionState:
    """Mutable state derived from ACP initialize and session responses."""

    _session_id: str | None = None
    _session_info: dict[str, Any] = field(default_factory=dict)
    _agent_capabilities: dict[str, Any] = field(default_factory=dict)
    _session_capabilities: dict[str, bool] = field(default_factory=dict)
    _root_uris: tuple[str, ...] = ()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def session_info(self) -> dict[str, Any]:
        return dict(self._session_info)

    @property
    def agent_capabilities(self) -> dict[str, Any]:
        return dict(self._agent_capabilities)

    @property
    def root_uris(self) -> tuple[str, ...]:
        return self._root_uris

    @property
    def session_capabilities(self) -> dict[str, bool]:
        return dict(self._session_capabilities)

    def update_agent_capabilities(self, capabilities: Any) -> None:
        self._agent_capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
        self._session_capabilities = parse_session_capabilities(self._agent_capabilities)

    def supports_session_load(self) -> bool:
        return self._agent_capabilities.get("loadSession") is True

    @property
    def supports_session_list(self) -> bool:
        return self._session_capabilities.get("list", False)

    @property
    def supports_session_resume(self) -> bool:
        return self._session_capabilities.get("resume", False)

    @property
    def supports_session_close(self) -> bool:
        return self._session_capabilities.get("close", False)

    @property
    def supports_session_delete(self) -> bool:
        return self._session_capabilities.get("delete", False)

    @property
    def supports_session_additional_directories(self) -> bool:
        return self._session_capabilities.get("additional_directories", False)

    def update_session_info(
        self,
        result: Any,
        *,
        fallback_session_id: str | None = None,
        fallback_roots: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        self._session_info = dict(result) if isinstance(result, dict) else {}
        self._session_id = extract_session_id(self._session_info) or fallback_session_id
        roots = extract_root_uris(self._session_info)
        if not roots and fallback_roots is not None:
            roots = tuple(root for root in fallback_roots if root)
        if roots:
            self._root_uris = roots
        return self.session_info

    def set_roots(self, roots: Iterable[str]) -> None:
        self._root_uris = tuple(root for root in roots if root)

    def clear_session(self) -> None:
        self._session_id = None
        self._session_info = {}
        self._root_uris = ()

    def reset(self) -> None:
        self.clear_session()
        self._agent_capabilities = {}
        self._session_capabilities = {}
