"""Thread-safe tool-context tracking for workflow hook parity."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, SessionSource

_TOOL_CONTEXT_REHYDRATION_SOURCES = frozenset(
    {
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.QWEN,
        SessionSource.DROID,
    }
)
_MAX_PENDING_TOOL_CONTEXTS_PER_SESSION = 100


class WorkflowToolContextMixin:
    """Own BEFORE/AFTER tool context correlation for workflow handlers."""

    def _initialize_tool_context_tracking(self) -> None:
        self._tool_context_lock = threading.Lock()
        self._tool_contexts: dict[str, list[dict[str, Any]]] = {}
        self._tool_context_by_id: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _tool_context_ids(data: dict[str, Any]) -> list[str]:
        """Extract stable per-tool identifiers from hook payloads."""
        identifiers: list[str] = []
        for key in (
            "tool_use_id",
            "toolUseId",
            "tool_call_id",
            "toolCallId",
            "call_id",
            "callId",
            "item_id",
            "itemId",
            "id",
        ):
            value = data.get(key)
            if isinstance(value, str) and value and value not in identifiers:
                identifiers.append(value)
        return identifiers

    @staticmethod
    def _tool_context_fingerprint(data: dict[str, Any]) -> str | None:
        """Return a content fingerprint for matching direct MCP proxy re-entry."""
        tool_name = data.get("tool_name") or data.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            return None

        tool_input = data.get("tool_input") or data.get("toolInput") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_input = deepcopy(tool_input)
        for arg_key in ("arguments", "args"):
            raw_args = tool_input.get(arg_key)
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(parsed_args, dict):
                    tool_input[arg_key] = parsed_args

        try:
            input_json = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
        except TypeError:
            input_json = repr(tool_input)
        return f"{tool_name}:{input_json}"

    @staticmethod
    def _needs_tool_rehydration(data: dict[str, Any]) -> bool:
        """Return True when an AFTER_TOOL event lacks usable tool context."""
        tool_name = data.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return True

        if data.get("tool_input") in (None, "", {}):
            return True

        if tool_name.startswith("mcp__") and (
            not data.get("mcp_server") or not data.get("mcp_tool")
        ):
            return True

        return False

    def _snapshot_tool_context(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Capture BEFORE_TOOL fields needed later on AFTER_TOOL."""
        tool_name = data.get("tool_name") or data.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            return None

        snapshot: dict[str, Any] = {"tool_name": tool_name}
        for key in (
            "tool_input",
            "mcp_server",
            "mcp_tool",
            "item_id",
            "itemId",
            "tool_use_id",
            "toolUseId",
            "tool_call_id",
            "toolCallId",
            "call_id",
            "callId",
            "id",
        ):
            value = data.get(key)
            if value not in (None, ""):
                snapshot[key] = deepcopy(value)

        identifiers = self._tool_context_ids(data)
        if identifiers:
            snapshot["_ids"] = identifiers

        fingerprint = self._tool_context_fingerprint(data)
        if fingerprint:
            snapshot["_fingerprint"] = fingerprint

        return snapshot

    @staticmethod
    def _tool_context_session_key(source: SessionSource, session_id: str) -> str:
        """Build a cache key that keeps CLI sources isolated."""
        return f"{source.value}:{session_id}"

    def _remember_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        data: dict[str, Any],
    ) -> None:
        """Store BEFORE_TOOL context until the matching AFTER_TOOL arrives."""
        snapshot = self._snapshot_tool_context(data)
        if snapshot is None:
            return

        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            pending = self._tool_contexts.setdefault(cache_key, [])
            pending.append(snapshot)
            if len(pending) > _MAX_PENDING_TOOL_CONTEXTS_PER_SESSION:
                evicted = pending.pop(0)
                for identifier in evicted.get("_ids", []):
                    id_key = (cache_key, identifier)
                    if self._tool_context_by_id.get(id_key) is evicted:
                        self._tool_context_by_id.pop(id_key, None)
            for identifier in snapshot.get("_ids", []):
                self._tool_context_by_id[(cache_key, identifier)] = snapshot

    def _match_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find the best stored BEFORE_TOOL context for an AFTER_TOOL event."""
        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            for identifier in self._tool_context_ids(data):
                snapshot = self._tool_context_by_id.get((cache_key, identifier))
                if snapshot is not None:
                    return snapshot

            pending = self._tool_contexts.get(cache_key, [])
            if not pending:
                return None

            tool_name = data.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                for snapshot in reversed(pending):
                    if snapshot.get("tool_name") == tool_name:
                        return snapshot

            return pending[-1]

    def _forget_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        """Remove stored BEFORE_TOOL context after the tool completes."""
        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            pending = self._tool_contexts.get(cache_key, [])
            if snapshot in pending:
                pending.remove(snapshot)
                if not pending:
                    self._tool_contexts.pop(cache_key, None)

            for identifier in snapshot.get("_ids", []):
                self._tool_context_by_id.pop((cache_key, identifier), None)

    def _clear_tool_context(self, source: SessionSource, session_id: str) -> None:
        """Drop any stored tool context for a session."""
        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            snapshots = self._tool_contexts.pop(cache_key, [])
            for snapshot in snapshots:
                for identifier in snapshot.get("_ids", []):
                    self._tool_context_by_id.pop((cache_key, identifier), None)

    def has_pending_tool_context(
        self,
        source: SessionSource,
        session_id: str,
        data: dict[str, Any],
    ) -> bool:
        """Return whether a matching CLI BEFORE_TOOL context is still pending."""
        fingerprint = self._tool_context_fingerprint(data)
        if not fingerprint:
            return False

        cache_key = self._tool_context_session_key(source, session_id)
        with self._tool_context_lock:
            return any(
                snapshot.get("_fingerprint") == fingerprint
                for snapshot in self._tool_contexts.get(cache_key, [])
            )

    def _sync_tool_context(self, event: HookEvent, session_id: str) -> None:
        """Maintain BEFORE/AFTER tool parity for rule evaluation."""
        if (
            event.source not in _TOOL_CONTEXT_REHYDRATION_SOURCES
            or not session_id
            or not isinstance(event.data, dict)
        ):
            return

        event_type = (
            event.event_type.value
            if isinstance(event.event_type, HookEventType)
            else str(event.event_type)
        )
        if event.event_type == HookEventType.SESSION_END or event_type in {
            HookEventType.AFTER_AGENT.value,
            HookEventType.STOP.value,
        }:
            self._clear_tool_context(event.source, session_id)
            return

        if event.event_type == HookEventType.BEFORE_TOOL:
            self._remember_tool_context(event.source, session_id, event.data)
            return

        if event.event_type != HookEventType.AFTER_TOOL:
            return

        snapshot = self._match_tool_context(event.source, session_id, event.data)
        if snapshot is None:
            return

        if self._needs_tool_rehydration(event.data):
            for key, value in snapshot.items():
                if key.startswith("_"):
                    continue
                if event.data.get(key) in (None, "", {}):
                    event.data[key] = deepcopy(value)

            from gobby.hooks.normalization import normalize_tool_fields

            normalize_tool_fields(event.data)
            event.metadata["_tool_context_rehydrated"] = True
            event.metadata["_tool_context_rehydrated_source"] = event.source.value
            if event.source == SessionSource.CODEX:
                event.metadata["_codex_tool_context_rehydrated"] = True

        self._forget_tool_context(event.source, session_id, snapshot)
