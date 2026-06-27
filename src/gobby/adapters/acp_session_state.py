"""ACP session state and capability tracking helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from gobby.adapters.acp_auth import normalize_auth_methods, supports_auth_logout
from gobby.adapters.acp_config_options import normalize_config_options

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


@dataclass
class ACPSessionState:
    """Mutable state derived from ACP initialize and session responses."""

    _session_id: str | None = None
    _session_info: dict[str, Any] = field(default_factory=dict)
    _agent_capabilities: dict[str, Any] = field(default_factory=dict)
    _auth_methods: tuple[dict[str, Any], ...] = ()
    _auth_logout_supported: bool = False
    _config_options: tuple[dict[str, Any], ...] = ()
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
    def auth_methods(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._auth_methods))

    @property
    def auth_logout_supported(self) -> bool:
        return self._auth_logout_supported

    @property
    def config_options(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._config_options))

    @property
    def root_uris(self) -> tuple[str, ...]:
        return self._root_uris

    def update_agent_capabilities(self, capabilities: Any) -> None:
        self._agent_capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
        self._auth_logout_supported = supports_auth_logout(self._agent_capabilities)

    def update_auth_methods(self, auth_methods: Any) -> list[dict[str, Any]]:
        self._auth_methods = tuple(normalize_auth_methods(auth_methods))
        return self.auth_methods

    def supports_session_load(self) -> bool:
        return self._agent_capabilities.get("loadSession") is True

    def update_session_info(
        self,
        result: Any,
        *,
        fallback_session_id: str | None = None,
        fallback_roots: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        self._session_info = dict(result) if isinstance(result, dict) else {}
        self._session_id = extract_session_id(self._session_info) or fallback_session_id
        self.update_config_options(self._session_info)
        roots = extract_root_uris(self._session_info)
        if not roots and fallback_roots is not None:
            roots = tuple(root for root in fallback_roots if root)
        if roots:
            self._root_uris = roots
        return self.session_info

    def update_config_options(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            self._config_options = tuple(normalize_config_options(payload.get("configOptions")))
        else:
            self._config_options = ()
        return self.config_options

    def set_roots(self, roots: Iterable[str]) -> None:
        self._root_uris = tuple(root for root in roots if root)

    def clear_session(self) -> None:
        self._session_id = None
        self._session_info = {}
        self._config_options = ()
        self._root_uris = ()

    def reset(self) -> None:
        self.clear_session()
        self._agent_capabilities = {}
        self._auth_methods = ()
        self._auth_logout_supported = False
