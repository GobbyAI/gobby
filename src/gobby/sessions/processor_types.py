"""Shared processor protocols and constants."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from gobby.sessions.message_stats import MessageStats
from gobby.sessions.transcript_index import TranscriptIndexAppender
from gobby.sessions.transcript_renderer import RenderState
from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage, TranscriptParser
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.token_events import TokenEventStore

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
    from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
    from gobby.storage.sessions import SessionManager
    from gobby.storage.unmodeled_observations import UnmodeledObservationStore


WINDOW_ONLY_CONTEXT_SOURCES = frozenset({"droid", "agy", "grok"})


class WebSocketServer(Protocol):
    async def broadcast(self, message: dict[str, Any]) -> None: ...

    async def broadcast_session_usage_updated(self, message: dict[str, Any]) -> None: ...

    async def broadcast_token_event(self, message: dict[str, Any]) -> None: ...

    async def feed_attached_session_tts(
        self,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool = False,
    ) -> None: ...


class ProcessorHost(Protocol):
    db: HubDatabase
    poll_interval: float
    websocket_server: WebSocketServer | None
    session_manager: SessionManager | None
    _hook_manager: HookManager | None
    _active_sessions: dict[str, str]
    _parsers: dict[str, TranscriptParser]
    _last_mtime: dict[str, float]
    _byte_offsets: dict[str, int]
    _message_indices: dict[str, int]
    _render_states: dict[str, RenderState]
    _index_appenders: dict[str, TranscriptIndexAppender]
    _observation_store: UnmodeledObservationStore
    _stats: dict[str, MessageStats]
    _stats_hydration_skipped: set[str]
    _running: bool
    _task: asyncio.Task[None] | None

    def _inc_counter(self, name: str) -> None: ...

    def _new_token_event_store(self) -> TokenEventStore: ...

    def _snapshot_from_token_usage(
        self,
        *,
        source: str | None,
        context_window: int | None,
        usage: TokenUsage,
        model: str | None,
    ) -> ContextUsageSnapshot | None: ...

    def _snapshot_from_window_metadata(
        self,
        *,
        source: str | None,
        context_window: int | None,
        model: str | None,
    ) -> ContextUsageSnapshot | None: ...

    async def _process_session(self, session_id: str, transcript_path: str) -> None: ...

    async def _process_json_session(self, session_id: str, transcript_path: str) -> None: ...

    async def _loop(self) -> None: ...

    async def _process_all_sessions(self) -> None: ...

    def _hydrate_registration_from_sidecar(
        self,
        session_id: str,
        transcript_path: str,
        source: str,
        appender: TranscriptIndexAppender,
    ) -> None: ...

    def _persist_appender_snapshot(
        self,
        session_id: str,
        transcript_path: str,
        appender: TranscriptIndexAppender,
        st: Any,
    ) -> None: ...

    def _accumulate_stats(self, session_id: str, messages: list[Any]) -> MessageStats: ...

    def _stats_from_session_manager(self, session_id: str) -> MessageStats: ...

    def _extract_native_titles(
        self, session_id: str, messages: list[ParsedMessage]
    ) -> list[ParsedMessage]: ...

    async def _persist_usage_events(
        self,
        session_id: str,
        messages: list[ParsedMessage],
    ) -> None: ...

    async def _feed_attached_session_tts(
        self,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool,
    ) -> None: ...

    async def _broadcast_rendered_session_message(
        self,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool,
    ) -> None: ...

    def _revive_expired_terminal_session(self, session_id: str) -> None: ...

    async def _render_and_broadcast_messages(
        self,
        session_id: str,
        messages: list[ParsedMessage],
        *,
        record_observations: bool = False,
    ) -> None: ...

    @staticmethod
    def _coerce_context_window(value: Any) -> int | None: ...

    @classmethod
    def _message_context_window(cls, message: ParsedMessage) -> int | None: ...

    @staticmethod
    def _usage_has_tokens(message: ParsedMessage) -> bool: ...
