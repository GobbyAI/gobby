"""ACP lifecycle operations for canonical Gobby sessions.

``ACPSessionLifecycleService`` drives ``session/close`` / ``session/delete``
for ACP-backed sessions already created through Gobby. Provider-native history
is intentionally not materialized as canonical Gobby sessions.

Broadcasts are never emitted here: ``update_status()`` and ``delete()`` fire
their own ``session_expired`` / ``session_deleted`` notifications. Emitting our
own would double-fire.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import psycopg

from gobby.adapters.acp_client import UnsupportedACPMethodError
from gobby.sessions.acp_session_mapping import (
    ACP_PROVIDERS,
    SESSION_TYPE_WEB_CHAT,
    build_acp_block,
    disposition_for_delete,
    normalize_additional_directories,
    status_for_close,
)

if TYPE_CHECKING:
    from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


class ACPLifecycleError(Exception):
    """Base class for ACP lifecycle failures the REST layer maps to status codes."""


class ACPSessionNotFoundError(ACPLifecycleError):
    """Unknown session id (REST → 404)."""


class ACPTargetNotSupportedError(ACPLifecycleError):
    """Target row is not an ACP session, e.g. tmux or a non-ACP provider (REST → 400)."""


class ACPProviderUnavailableError(ACPLifecycleError):
    """Provider backend is unavailable / not started (REST → 503)."""


class ACPCapabilityUnsupportedError(ACPLifecycleError):
    """Agent does not advertise the requested lifecycle capability (REST → 409)."""

    def __init__(self, method: str) -> None:
        self.method = method
        super().__init__(f"ACP agent does not support {method}")


class ACPWorkspaceIdentityError(ACPLifecycleError):
    """Persisted workspace identity is absent, tombstoned, or stale (fail closed)."""


def _acp_provider_names(runtime_manager: WebChatRuntimeManager | None) -> frozenset[str]:
    """Resolve the ACP provider set, preferring the live runtime registry."""
    if runtime_manager is not None:
        try:
            return frozenset(runtime_manager.acp_backends().keys())
        except Exception:  # pragma: no cover - defensive; registry is a plain dict
            logger.debug("acp_backends() registry read failed; using fallback", exc_info=True)
    return frozenset(ACP_PROVIDERS)


def is_acp_session(session: Any, runtime_manager: WebChatRuntimeManager | None) -> bool:
    """True when a canonical row is an ACP-backed web-chat session."""
    if getattr(session, "session_type", None) != SESSION_TYPE_WEB_CHAT:
        return False
    source = getattr(session, "source", None)
    return bool(source) and source in _acp_provider_names(runtime_manager)


def attach_acp_block(
    session_data: dict[str, Any],
    session: Any,
    runtime_manager: WebChatRuntimeManager | None,
) -> None:
    """Attach the normalized ``acp`` enrichment block to a serialized session.

    Present for every ACP web-chat row (grok/qwen) so the UI's
    ``Boolean(session.acp)`` detection is stable; capabilities are empty when the
    agent advertises none (graceful degradation: chip shows, zero buttons). No-op
    for non-ACP rows so the block is absent there.
    """
    if not is_acp_session(session, runtime_manager):
        return
    source = session.source
    capabilities: Mapping[str, bool] = (
        runtime_manager.acp_session_capabilities(source) if runtime_manager else {}
    )
    additional_directories: tuple[str, ...] = ()
    external_id = getattr(session, "external_id", None)
    if runtime_manager is not None and external_id:
        info = runtime_manager.get_acp_session_info(source, external_id)
        if info is not None:
            additional_directories = normalize_additional_directories(
                info.get("additionalDirectories")
            )
    session_data["acp"] = build_acp_block(
        capabilities, additional_directories=additional_directories
    )


class ACPSessionLifecycleService:
    """Close and delete ACP sessions already registered as canonical Gobby rows."""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        runtime_manager: WebChatRuntimeManager | None,
    ) -> None:
        self._session_manager = session_manager
        self._runtime_manager = runtime_manager

    # -- close / delete ----------------------------------------------------

    async def close(self, session_id: str) -> dict[str, Any]:
        """Close an ACP session: ``session/close`` then transition the row to ``expired``."""
        session = self._require_session(session_id)
        provider, external_id = self._acp_target(session)
        try:
            await self._with_operation_client(
                session,
                provider,
                lambda client: client.close_session(external_id),
                capability="close",
            )
        except UnsupportedACPMethodError as exc:
            raise ACPCapabilityUnsupportedError("session/close") from exc

        # Reuse the canonical expire transition: sets ``expired`` and emits
        # ``session_expired`` (no manual broadcast).
        self._session_manager.update_status(session_id, status_for_close())
        updated = self._session_manager.get(session_id) or session
        return {"session": self._serialize(updated)}

    async def delete(self, session_id: str) -> dict[str, Any]:
        """Delete an ACP session: ``session/delete`` then hard-remove the row.

        On an FK integrity error (tasks / agent_runs reference ``sessions``
        without cascade) fall back to the expire transition.
        """
        session = self._require_session(session_id)
        provider, external_id = self._acp_target(session)
        try:
            await self._with_operation_client(
                session,
                provider,
                lambda client: client.delete_session(external_id),
                capability="delete",
            )
        except UnsupportedACPMethodError as exc:
            raise ACPCapabilityUnsupportedError("session/delete") from exc

        try:
            deleted = self._session_manager.delete(session_id)
        except psycopg.IntegrityError as exc:
            logger.warning("ACP delete FK fallback to expire for session %s: %s", session_id, exc)
            self._session_manager.update_status(session_id, status_for_close())
            updated = self._session_manager.get(session_id) or session
            return {"session": self._serialize(updated), "disposition": status_for_close()}

        if not deleted:
            raise ACPSessionNotFoundError(session_id)
        # The row is gone; return its pre-delete snapshot as confirmation. The
        # frontend removes the row off the ``session_deleted`` broadcast.
        return {"session": self._serialize(session), "disposition": disposition_for_delete()}

    # -- helpers -----------------------------------------------------------

    def _require_session(self, session_id: str) -> Session:
        session = self._session_manager.get(session_id)
        if session is None:
            raise ACPSessionNotFoundError(session_id)
        return session

    def _acp_target(self, session: Session) -> tuple[str, str]:
        external_id = getattr(session, "external_id", None)
        if not is_acp_session(session, self._runtime_manager) or not external_id:
            raise ACPTargetNotSupportedError(getattr(session, "id", None))
        return session.source, external_id

    def _require_workspace(self, session: Session) -> tuple[str, int]:
        path = getattr(session, "workspace_path", None)
        if not isinstance(path, str) or not path.strip():
            raise ACPWorkspaceIdentityError("session workspace identity is absent")
        generation = int(getattr(session, "workspace_generation", 0) or 0)
        return path, generation

    async def _with_operation_client(
        self,
        session: Session,
        provider: str,
        operation: Any,
        *,
        capability: str,
    ) -> Any:
        backend = self._require_available_backend(provider)
        path, generation = self._require_workspace(session)
        current = self._session_manager.get(session.id)
        if current is None or int(getattr(current, "workspace_generation", 0) or 0) != generation:
            raise ACPWorkspaceIdentityError("session workspace identity changed before launch")
        client = backend.acp_client_cls(
            cwd=path,
            sandbox_config=getattr(backend, "_sandbox_config", None),
            sandbox_run_id=str(session.id),
        )
        try:
            try:
                await client.start(auto_session=False, cwd=path)
            except FileNotFoundError as exc:
                raise ACPProviderUnavailableError(provider) from exc
            current = self._session_manager.get(session.id)
            if (
                current is None
                or int(getattr(current, "workspace_generation", 0) or 0) != generation
            ):
                raise ACPWorkspaceIdentityError("session workspace identity changed during launch")
            if not client.session_capabilities.get(capability):
                raise ACPCapabilityUnsupportedError(f"session/{capability}")
            return await operation(client)
        finally:
            try:
                await client.stop()
            except Exception:
                logger.debug(
                    "ACP operation-owned client stop failed",
                    extra={"session_id": session.id},
                    exc_info=True,
                )

    def _require_available_backend(self, provider: str) -> Any:
        backend = self._runtime_manager.acp_backend(provider) if self._runtime_manager else None
        if backend is None or not backend.health().available:
            raise ACPProviderUnavailableError(provider)
        return backend

    def _capabilities(self, provider: str) -> dict[str, bool]:
        if self._runtime_manager is None:
            return {}
        return self._runtime_manager.acp_session_capabilities(provider)

    def _require_capability(self, provider: str, capability: str) -> None:
        if not self._capabilities(provider).get(capability):
            raise ACPCapabilityUnsupportedError(f"session/{capability}")

    def _serialize(self, session: Session) -> dict[str, Any]:
        data = session.to_dict()
        attach_acp_block(data, session, self._runtime_manager)
        return data


__all__ = [
    "ACPCapabilityUnsupportedError",
    "ACPLifecycleError",
    "ACPProviderUnavailableError",
    "ACPSessionLifecycleService",
    "ACPSessionNotFoundError",
    "ACPTargetNotSupportedError",
    "ACPWorkspaceIdentityError",
    "attach_acp_block",
    "is_acp_session",
]
