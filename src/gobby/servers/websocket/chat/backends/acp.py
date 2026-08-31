"""Shared ACP web-chat backend.

Hosts a single subprocess speaking the Agent Communication Protocol and
multiplexes session attach/detach/send across managed chat sessions. Per-CLI
concretes (``GrokWebChatBackend``, ``QwenWebChatBackend``) override the
class attributes ``provider``, ``display_name``, ``start_timeout_seconds``,
and ``acp_client_cls``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from gobby.adapters.acp_client import ACPClient, StreamEvent
from gobby.adapters.acp_commands import normalize_available_commands
from gobby.agents.trust import pre_approve_directory
from gobby.config.ai import GenerationEndpointConfig
from gobby.servers.websocket.chat.backends.base import (
    _BACKEND_START_TIMEOUT_SECONDS,
    ProviderBackendHealth,
    launch_sandbox_config,
)

if TYPE_CHECKING:
    from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession

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
        local_generation_endpoints: dict[str, GenerationEndpointConfig] | None = None,
    ) -> None:
        if not self.provider or not self.display_name:
            raise TypeError(
                f"{type(self).__name__} must set provider and display_name class attributes"
            )

        self._local_generation_endpoints = dict(local_generation_endpoints or {})
        # ACP CLI bootstrap currently hangs on macOS when launched
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
        cli_name = getattr(self.acp_client_cls, "cli_name", "")
        if shutil.which(cli_name):
            self._health = ProviderBackendHealth(provider=self.provider, available=True)
            return
        self._health = ProviderBackendHealth(
            provider=self.provider,
            available=False,
            startup_error=f"{self.display_name} CLI not found in PATH",
        )

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

    @property
    def session_capabilities(self) -> dict[str, bool]:
        """ACP session lifecycle capabilities advertised by this provider's agent."""
        return self._client.session_capabilities

    @property
    def agent_capabilities(self) -> dict[str, Any]:
        """ACP agent capabilities advertised by this provider."""
        return self._client.agent_capabilities

    async def list_sessions(
        self,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List agent-side ACP sessions on the warm shared client."""
        return await self._client.list_sessions(cwd=cwd, cursor=cursor)

    async def resume_session(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        additional_directories: list[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Resume an agent-side ACP session on the warm shared client."""
        return await self._client.resume_session(
            session_id,
            cwd=cwd,
            additional_directories=additional_directories,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    async def close_session(self, session_id: str) -> dict[str, Any]:
        """Close an agent-side ACP session on the warm shared client."""
        return await self._client.close_session(session_id)

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        """Delete an agent-side ACP session on the warm shared client."""
        return await self._client.delete_session(session_id)

    async def attach_session(
        self,
        session: ACPManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            session._model = model
        elif not session._model:
            session._model = self._default_model
        if not session._model or not session._model.strip():
            raise RuntimeError(
                f"{self.display_name} model could not be resolved before ACP session creation"
            )

        await self.start()
        if not self._health.available:
            raise RuntimeError(
                self._health.startup_error or f"{self.display_name} backend unavailable"
            )

        session_id = session.sdk_session_id or session.resume_session_id
        cwd = str(Path(session.project_path or ".").expanduser().resolve())
        pre_approve_directory(self.provider, cwd)
        if self._client.is_started:
            client = self._client
        else:
            client = self.acp_client_cls(
                cwd=cwd,
                sandbox_config=launch_sandbox_config(session),
                sandbox_run_id=session.db_session_id or session.conversation_id,
            )
        session._acp_client = client
        if not client.is_started:
            try:
                await client.start(
                    auto_session=False,
                    cwd=cwd,
                    model=session._model,
                    reasoning_effort=session.reasoning_effort,
                )
            except Exception:
                session._acp_client = None
                try:
                    await client.stop()
                except Exception:
                    logger.debug(
                        "%s session client cleanup after failed start",
                        self.display_name,
                        exc_info=True,
                    )
                raise
        # Re-establish an existing session preferring session/resume, since
        # Gobby re-renders the transcript from its own DB on continue_in_chat:
        #   resume (no agent-side replay) -> load (replays history) -> new.
        if not session_id:
            session_info = await client.create_session(
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )
        elif client.session_capabilities.get("resume"):
            session_info = await client.resume_session(
                session_id,
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )
        elif client.agent_capabilities.get("loadSession") is True:
            session_info = await client.load_session(
                session_id,
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )
        else:
            session_info = await client.create_session(
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )

        resolved_session_id = (
            session_info.get("sessionId")
            or session_info.get("session_id")
            or client.session_id
            or session_id
        )
        if isinstance(resolved_session_id, str) and resolved_session_id:
            session.sdk_session_id = resolved_session_id
        session.available_commands = normalize_available_commands(
            session_info.get("availableCommands")
        )
        session._connected = True
        session.last_activity = datetime.now(UTC)

    async def detach_session(self, session: ACPManagedChatSession) -> None:
        session._connected = False
        client = session._acp_client
        session._acp_client = None
        if client is not None:
            try:
                await client.stop()
            except Exception:
                logger.debug("%s session client stop failed", self.display_name, exc_info=True)

    async def send_message(
        self,
        session: ACPManagedChatSession,
        prompt: str | list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        if not self._health.available:
            raise RuntimeError(
                self._health.startup_error or f"{self.display_name} backend unavailable"
            )
        if not session.sdk_session_id:
            raise RuntimeError(f"{self.display_name} session missing sessionId")

        async def _apply_pre_tool(data: dict[str, Any]) -> dict[str, Any] | None:
            tool_name = data.get("tool_name")
            tool_input = data.get("tool_input")
            if not isinstance(tool_name, str):
                logger.warning(
                    "%s emitted non-string tool_name %r; using safe empty name",
                    self.display_name,
                    tool_name,
                )
                tool_name = ""
            return await session._apply_pre_tool_lifecycle(
                tool_name,
                tool_input if isinstance(tool_input, dict) else {},
            )

        client = session._acp_client
        if client is None:
            raise RuntimeError(f"{self.display_name} session has no ACP client")
        async for event in client.send(
            prompt,
            session_id=session.sdk_session_id,
            model=session._model,
            reasoning_effort=session.reasoning_effort,
            pre_tool_callback=_apply_pre_tool,
        ):
            yield event

    async def interrupt(self, session: ACPManagedChatSession) -> None:
        client = session._acp_client
        if client is None:
            return
        await client.cancel_session(session.sdk_session_id)

    async def switch_model(self, session: ACPManagedChatSession, new_model: str) -> None:
        session._model = new_model
        session._connected = False


__all__ = ["ACPWebChatBackend"]
