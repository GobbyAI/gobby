"""Shared ACP web-chat backend.

Hosts a single subprocess speaking the Agent Communication Protocol and
multiplexes session attach/detach/send across managed chat sessions. Per-CLI
concretes (``GeminiWebChatBackend``, ``QwenWebChatBackend``) override the
class attributes ``provider``, ``display_name``, ``start_timeout_seconds``,
and ``acp_client_cls``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from gobby.adapters.acp_client import ACPClient, StreamEvent
from gobby.agents.sandbox import SandboxConfig
from gobby.servers.websocket.chat.backends.base import (
    _BACKEND_START_TIMEOUT_SECONDS,
    ProviderBackendHealth,
    _error_message,
)

if TYPE_CHECKING:
    from gobby.servers.websocket.chat.backends.gemini import GeminiManagedChatSession

logger = logging.getLogger(__name__)


class ACPWebChatBackend:
    """Daemon-owned shared ACP backend, multi-session capable."""

    provider: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    start_timeout_seconds: ClassVar[float] = _BACKEND_START_TIMEOUT_SECONDS
    acp_client_cls: ClassVar[type[ACPClient]]

    def __init__(
        self,
        *,
        client: ACPClient | None = None,
        default_model: str | None = None,
        sandbox_config: SandboxConfig | None = None,
    ) -> None:
        if not self.provider or not self.display_name:
            raise TypeError(
                f"{type(self).__name__} must set provider and display_name class attributes"
            )

        self._sandbox_config = sandbox_config
        # Gemini-compatible CLI ACP bootstrap currently hangs on macOS when launched
        # with daemon-wide Seatbelt flags. Keep daemon-owned ACP startup unsandboxed
        # and let the upstream CLI's own tool sandboxing handle tool execution.
        self._client: ACPClient = client or self.acp_client_cls()
        self._health = ProviderBackendHealth(provider=self.provider, available=False)
        self._default_model = default_model
        self._startup_task: asyncio.Task[None] | None = None

    async def _start_inner(self) -> None:
        if self._client.is_started:
            self._health = ProviderBackendHealth(provider=self.provider, available=True)
            return

        try:
            await asyncio.wait_for(
                self._client.start(
                    auto_session=False,
                    model=self._default_model,
                ),
                timeout=self.start_timeout_seconds,
            )
        except Exception as exc:
            startup_error = _error_message(exc)
            if isinstance(exc, TimeoutError) and startup_error == "TimeoutError":
                startup_error = (
                    f"Timed out starting {self.display_name} ACP backend after "
                    f"{self.start_timeout_seconds:.1f}s"
                )
            try:
                await self._client.stop()
            except Exception:
                logger.debug(
                    "%s backend cleanup after failed startup", self.display_name, exc_info=True
                )
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error=startup_error,
            )
            logger.warning("%s ACP backend startup failed: %s", self.display_name, startup_error)
            return

        self._health = ProviderBackendHealth(provider=self.provider, available=True)

    async def start(self, *, background: bool = False) -> None:
        if self._health.available:
            return
        if self._startup_task and not self._startup_task.done():
            if not background:
                await self._startup_task
            return

        self._startup_task = asyncio.create_task(self._start_inner())
        if not background:
            await self._startup_task

    async def stop(self) -> None:
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass
        self._startup_task = None
        if self._client.is_started:
            await self._client.stop()
        self._health = ProviderBackendHealth(provider=self.provider, available=False)

    def health(self) -> ProviderBackendHealth:
        return self._health

    async def attach_session(
        self,
        session: GeminiManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            session._model = model
        elif not session._model:
            session._model = self._default_model

        await self.start()
        if not self._health.available:
            raise RuntimeError(
                self._health.startup_error or f"{self.display_name} backend unavailable"
            )

        session_id = session.sdk_session_id or session.resume_session_id
        cwd = session.project_path or "."
        if session_id:
            session_info = await self._client.load_session(
                session_id,
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )
        else:
            session_info = await self._client.create_session(
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )

        resolved_session_id = (
            session_info.get("sessionId")
            or session_info.get("session_id")
            or self._client.session_id
            or session_id
        )
        if isinstance(resolved_session_id, str) and resolved_session_id:
            session.sdk_session_id = resolved_session_id
        session._connected = True
        session.last_activity = datetime.now(UTC)

    async def detach_session(self, session: GeminiManagedChatSession) -> None:
        session._connected = False

    async def send_message(
        self,
        session: GeminiManagedChatSession,
        prompt: str,
    ) -> AsyncIterator[StreamEvent]:
        if not self._health.available:
            raise RuntimeError(
                self._health.startup_error or f"{self.display_name} backend unavailable"
            )
        if not session.sdk_session_id:
            raise RuntimeError(f"{self.display_name} session missing sessionId")

        async for event in self._client.send(
            prompt,
            session_id=session.sdk_session_id,
            model=session._model,
            reasoning_effort=session.reasoning_effort,
        ):
            yield event

    async def switch_model(self, session: GeminiManagedChatSession, new_model: str) -> None:
        session._model = new_model
        session._connected = False


__all__ = ["ACPWebChatBackend"]
