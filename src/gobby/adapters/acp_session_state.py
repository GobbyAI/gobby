"""ACP session state and capability tracking helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

DEFAULT_ACP_CLIENT_CAPABILITIES: Mapping[str, bool] = MappingProxyType({"terminal": True})


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


@dataclass
class ACPSessionState:
    """Mutable state derived from ACP initialize and session responses."""

    _session_id: str | None = None
    _session_info: dict[str, Any] = field(default_factory=dict)
    _agent_capabilities: dict[str, Any] = field(default_factory=dict)
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

    def update_agent_capabilities(self, capabilities: Any) -> None:
        self._agent_capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}

    def supports_session_load(self) -> bool:
        return self._agent_capabilities.get("loadSession") is True

    def update_session_info(
        self,
        result: Any,
        *,
        fallback_session_id: str | None = None,
    ) -> dict[str, Any]:
        self._session_info = dict(result) if isinstance(result, dict) else {}
        self._session_id = extract_session_id(self._session_info) or fallback_session_id
        roots = extract_root_uris(self._session_info)
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
