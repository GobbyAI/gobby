"""ACP session discovery + lifecycle as canonical Gobby sessions.

``ACPSessionLifecycleService`` reconciles agent-side ACP sessions into the
canonical ``sessions`` table and drives ``session/close`` / ``session/delete``
through the same store, so ACP-backed sessions live in the existing Sessions
panel alongside TMUX and WEB rows. All ACP protocol vocabulary is translated by
``acp_session_mapping`` before it reaches this layer; this service only
orchestrates the warm ACP backend, the ``SessionManager`` CRUD seam, and the
status transitions ACP lifecycle outcomes map onto.

Broadcasts are never emitted here: ``register()``, ``update_title()``,
``update_status()`` and ``delete()`` each fire their own
``session_created`` / ``session_updated`` / ``session_expired`` /
``session_deleted`` notifications. Emitting our own would double-fire.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import psycopg

from gobby.adapters.acp_client import UnsupportedACPMethodError
from gobby.sessions.acp_session_mapping import (
    ACP_PROVIDERS,
    SESSION_TYPE_WEB_CHAT,
    MappedSessionInfo,
    build_acp_block,
    disposition_for_delete,
    map_session_info,
    normalize_additional_directories,
    status_for_close,
)
from gobby.storage.sessions._title_defaults import PROVISIONAL_TITLE_SOURCE

if TYPE_CHECKING:
    from gobby.servers.websocket.chat.runtime_manager import WebChatRuntimeManager
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

# Bound the ``session/list`` pagination walk so a misbehaving agent cannot pin
# the discover loop indefinitely.
ACP_DISCOVER_PAGE_CAP = 20

# Title source recorded when an ACP-provided title upgrades a provisional row.
# ACP session/list titles are provider-native; "manual" is reserved for user renames.
_ACP_TITLE_SOURCE = "native"


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
    """Discover, close, and delete ACP sessions as canonical Gobby rows."""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        runtime_manager: WebChatRuntimeManager | None,
        resolve_project_id: Callable[[str | None], str | None],
        machine_id: str,
        page_cap: int = ACP_DISCOVER_PAGE_CAP,
    ) -> None:
        self._session_manager = session_manager
        self._runtime_manager = runtime_manager
        self._resolve_project_id = resolve_project_id
        self._machine_id = machine_id
        self._page_cap = max(1, page_cap)
        # Per-provider/cwd in-flight scan tasks. Concurrent discover calls join
        # the matching scan instead of hammering the ACP subprocess again.
        self._inflight: dict[tuple[str, str | None], asyncio.Task[dict[str, Any]]] = {}

    # -- discovery ---------------------------------------------------------

    async def discover(self, *, cwd: str | None = None) -> dict[str, Any]:
        """Reconcile agent-side ACP sessions into canonical rows.

        Returns a discovery summary: ``{sessions, skipped, providers}``. Per-row
        and per-provider failures are collected into ``skipped`` / surfaced via
        the ``providers`` summary rather than failing the whole call.
        """
        sessions: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        providers: list[dict[str, Any]] = []

        runtime_manager = self._runtime_manager
        if runtime_manager is None:
            return {"sessions": sessions, "skipped": skipped, "providers": providers}

        for provider, backend in runtime_manager.acp_backends().items():
            scan = await self._scan_provider(provider, backend, cwd)
            sessions.extend(scan["sessions"])
            skipped.extend(scan["skipped"])
            providers.append(
                {
                    "provider": provider,
                    "available": scan["available"],
                    "supports_list": scan["supports_list"],
                    "truncated": scan.get("truncated", False),
                }
            )
        return {"sessions": sessions, "skipped": skipped, "providers": providers}

    async def _scan_provider(self, provider: str, backend: Any, cwd: str | None) -> dict[str, Any]:
        """Coalesce concurrent scans of one provider onto a single in-flight task."""
        key = (provider, cwd)
        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            return await existing
        task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
            self._scan_provider_inner(provider, backend, cwd)
        )
        self._inflight[key] = task
        try:
            return await task
        finally:
            if self._inflight.get(key) is task:
                del self._inflight[key]

    async def _scan_provider_inner(
        self, provider: str, backend: Any, cwd: str | None
    ) -> dict[str, Any]:
        sessions: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        try:
            await backend.start()
        except Exception as exc:
            logger.warning("ACP %s backend start failed during discovery: %s", provider, exc)
            skipped.append({"provider": provider, "reason": "provider_start_failed"})
            return {
                "sessions": sessions,
                "skipped": skipped,
                "available": False,
                "supports_list": False,
                "truncated": False,
            }

        if not backend.health().available:
            skipped.append({"provider": provider, "reason": "provider_unavailable"})
            return {
                "sessions": sessions,
                "skipped": skipped,
                "available": False,
                "supports_list": False,
                "truncated": False,
            }

        capabilities = self._capabilities(provider)
        if not capabilities.get("list"):
            return {
                "sessions": sessions,
                "skipped": skipped,
                "available": True,
                "supports_list": False,
                "truncated": False,
            }

        cursor: str | None = None
        pages = 0
        truncated = False
        while pages < self._page_cap:
            pages += 1
            try:
                result = await backend.list_sessions(cwd=cwd, cursor=cursor)
            except UnsupportedACPMethodError:
                return {
                    "sessions": sessions,
                    "skipped": skipped,
                    "available": True,
                    "supports_list": False,
                    "truncated": False,
                }
            except Exception as exc:
                logger.warning("ACP %s session/list failed: %s", provider, exc)
                skipped.append({"provider": provider, "reason": "list_failed"})
                break

            for info in result.get("sessions") or []:
                self._process_info(provider, info, sessions, skipped)

            cursor = result.get("nextCursor")
            if not cursor:
                break
            if pages >= self._page_cap:
                truncated = True
                skipped.append({"provider": provider, "reason": "page_cap_reached"})
                break

        return {
            "sessions": sessions,
            "skipped": skipped,
            "available": True,
            "supports_list": True,
            "truncated": truncated,
        }

    def _process_info(
        self,
        provider: str,
        info: Any,
        sessions: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> None:
        """Map and upsert one ``SessionInfo``, applying per-row resilience."""
        mapped = map_session_info(
            info, provider=provider, resolve_project_id=self._resolve_project_id
        )
        raw_session_id = info.get("sessionId") if isinstance(info, Mapping) else None
        if mapped is None:
            skipped.append(
                {
                    "provider": provider,
                    "session_id": raw_session_id,
                    "reason": "invalid_session_info",
                }
            )
            return
        if mapped.project_id is None:
            skipped.append(
                {
                    "provider": provider,
                    "session_id": mapped.external_id,
                    "reason": "unresolved_cwd",
                }
            )
            return

        if self._runtime_manager is not None and isinstance(info, Mapping):
            self._runtime_manager.cache_acp_session_info(provider, mapped.external_id, dict(info))
        try:
            session = self._upsert(provider, mapped)
        except Exception as exc:
            logger.warning(
                "ACP %s upsert failed for session %s: %s", provider, mapped.external_id, exc
            )
            skipped.append(
                {
                    "provider": provider,
                    "session_id": mapped.external_id,
                    "reason": "upsert_failed",
                }
            )
            return
        sessions.append(self._serialize(session))

    def _upsert(self, provider: str, mapped: MappedSessionInfo) -> Session:
        """Conservative upsert: never move an existing row; only refresh provisional titles."""
        existing = self._session_manager.find_by_external_id(
            mapped.external_id,
            self._machine_id,
            mapped.project_id,
            provider,
            session_type=SESSION_TYPE_WEB_CHAT,
        )
        if existing is not None:
            if (
                mapped.title
                and self._title_is_provisional(existing)
                and existing.title != mapped.title
            ):
                updated = self._session_manager.update_title(
                    existing.id, mapped.title, title_source=_ACP_TITLE_SOURCE
                )
                return updated or existing
            return existing
        return self._session_manager.register(
            external_id=mapped.external_id,
            machine_id=self._machine_id,
            source=provider,
            project_id=mapped.project_id,
            title=mapped.title,
            session_type=SESSION_TYPE_WEB_CHAT,
            title_source=_ACP_TITLE_SOURCE if mapped.title else None,
        )

    @staticmethod
    def _title_is_provisional(session: Session) -> bool:
        if (session.title_source or "") == PROVISIONAL_TITLE_SOURCE:
            return True
        return not (session.title or "").strip()

    # -- close / delete ----------------------------------------------------

    async def close(self, session_id: str) -> dict[str, Any]:
        """Close an ACP session: ``session/close`` then transition the row to ``expired``."""
        session = self._require_session(session_id)
        provider, external_id = self._acp_target(session)
        backend = self._require_available_backend(provider)
        self._require_capability(provider, "close")
        try:
            await backend.close_session(external_id)
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
        backend = self._require_available_backend(provider)
        self._require_capability(provider, "delete")
        try:
            await backend.delete_session(external_id)
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
    "ACP_DISCOVER_PAGE_CAP",
    "ACPCapabilityUnsupportedError",
    "ACPLifecycleError",
    "ACPProviderUnavailableError",
    "ACPSessionLifecycleService",
    "ACPSessionNotFoundError",
    "ACPTargetNotSupportedError",
    "attach_acp_block",
    "is_acp_session",
]
