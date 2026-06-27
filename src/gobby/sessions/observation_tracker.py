"""Run-scoped unmodeled transcript observation tracking."""

from __future__ import annotations

import logging
from typing import Any

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.unmodeled_observations import (
    UnmodeledObservationInput,
    UnmodeledObservationStore,
    sample_keys,
    stable_sample_hash,
)

logger = logging.getLogger("gobby.sessions.unmodeled_observations")

_SYNTHETIC_TOOL_NAMES = frozenset({"", "unknown", "unknown_result"})


class ObservationTracker:
    """Deduplicate unmodeled observation writes within one render/parse run."""

    def __init__(self, store: UnmodeledObservationStore | None = None) -> None:
        self._store = store
        self._seen: set[tuple[str, str, str, str, str, str, str, str]] = set()

    def observe_block_type(
        self,
        msg: ParsedMessage,
        *,
        session_id: str | None,
        source: str | None,
        block_type: str,
    ) -> None:
        self._observe(
            msg,
            session_id=session_id,
            source=source,
            kind="block_type",
            name=block_type,
            server_name="",
            tool_type="",
            sample=msg.raw_json,
        )

    def observe_tool_name(
        self,
        msg: ParsedMessage,
        *,
        session_id: str | None,
        source: str | None,
        tool_name: str,
        server_name: str,
        tool_type: str,
    ) -> None:
        if tool_name in _SYNTHETIC_TOOL_NAMES:
            return
        self._observe(
            msg,
            session_id=session_id,
            source=source,
            kind="tool_name",
            name=tool_name,
            server_name=server_name,
            tool_type=tool_type,
            sample=msg.tool_input or msg.raw_json,
        )

    def _observe(
        self,
        msg: ParsedMessage,
        *,
        session_id: str | None,
        source: str | None,
        kind: str,
        name: str,
        server_name: str,
        tool_type: str,
        sample: dict[str, Any],
    ) -> None:
        resolved_session_id = session_id or "unknown"
        resolved_source = msg.source or source or "unknown"
        source_ref = msg.source_ref or (
            str(msg.source_line) if msg.source_line is not None else str(msg.index)
        )
        source_line = msg.source_line if msg.source_line is not None else msg.index
        sample_hash = stable_sample_hash(sample)
        key = (
            resolved_session_id,
            resolved_source,
            kind,
            name,
            server_name,
            tool_type,
            source_ref,
            sample_hash,
        )
        if key in self._seen:
            return
        self._seen.add(key)

        logger.info(
            "Unmodeled transcript block observed",
            extra={
                "session_id": resolved_session_id,
                "source": resolved_source,
                "kind": kind,
                "name": name,
                "server_name": server_name,
                "tool_type": tool_type,
                "source_ref": source_ref,
                "source_line": source_line,
                "sample_keys": sample_keys(sample),
                "sample_hash": sample_hash,
            },
        )

        if self._store is None:
            return

        try:
            self._store.record(
                UnmodeledObservationInput(
                    session_id=resolved_session_id,
                    source=resolved_source,
                    kind=kind,
                    name=name,
                    server_name=server_name,
                    tool_type=tool_type,
                    source_ref=source_ref,
                    source_line=source_line,
                    sample=sample,
                )
            )
        except Exception:
            logger.debug(
                "Failed to persist unmodeled transcript observation",
                extra={
                    "session_id": resolved_session_id,
                    "source": resolved_source,
                    "kind": kind,
                    "name": name,
                },
                exc_info=True,
            )


def is_synthetic_tool_name(tool_name: str | None) -> bool:
    return (tool_name or "") in _SYNTHETIC_TOOL_NAMES
