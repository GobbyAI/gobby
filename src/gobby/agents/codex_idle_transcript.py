"""Redacted Codex transcript snapshots for idle-agent diagnostics."""

from __future__ import annotations

import asyncio
import json
from collections import deque

from gobby.agents.idle_check_models import (
    _CodexTranscriptEventSummary,
    _CodexTranscriptSnapshot,
)

CODEX_MODEL_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."


async def read_codex_transcript_snapshot(
    path: str,
    *,
    limit: int = 8,
) -> _CodexTranscriptSnapshot:
    """Read a bounded, content-free summary of a Codex rollout transcript."""

    def _read() -> _CodexTranscriptSnapshot:
        items: deque[_CodexTranscriptEventSummary] = deque(maxlen=limit)
        lifecycle_event: _CodexTranscriptEventSummary | None = None
        task_started_event: _CodexTranscriptEventSummary | None = None
        capacity_error_event: _CodexTranscriptEventSummary | None = None
        latest_model_output_line_num: int | None = None
        last_malformed_line_num: int | None = None
        with open(path, encoding="utf-8") as handle:
            for line_num, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    last_malformed_line_num = line_num
                    continue
                if not isinstance(data, dict):
                    last_malformed_line_num = line_num
                    continue
                event_type = data.get("type")
                if not isinstance(event_type, str):
                    last_malformed_line_num = line_num
                    continue
                if event_type not in {"response_item", "event_msg"}:
                    continue
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    last_malformed_line_num = line_num
                    continue
                payload_type = payload.get("type")
                if not isinstance(payload_type, str):
                    last_malformed_line_num = line_num
                    continue
                raw_timestamp = data.get("timestamp")
                timestamp = raw_timestamp if isinstance(raw_timestamp, str) else None
                summary = _CodexTranscriptEventSummary(
                    line_num=line_num,
                    timestamp=timestamp,
                    event_type=event_type,
                    payload_type=payload_type,
                )
                if event_type == "response_item":
                    items.append(summary)
                    if payload_type != "message" or payload.get("role") != "user":
                        latest_model_output_line_num = line_num
                else:
                    if payload_type in {"agent_message", "agent_reasoning"}:
                        latest_model_output_line_num = line_num
                    if payload_type == "task_started":
                        task_started_event = summary
                    if payload_type in {"task_started", "task_complete"}:
                        lifecycle_event = summary
                    if (
                        payload_type == "error"
                        and payload.get("message") == CODEX_MODEL_CAPACITY_MESSAGE
                        and payload.get("codex_error_info") == "server_overloaded"
                    ):
                        capacity_error_event = summary
        return _CodexTranscriptSnapshot(
            response_items=tuple(items),
            lifecycle_event=lifecycle_event,
            task_started_event=task_started_event,
            capacity_error_event=capacity_error_event,
            latest_model_output_line_num=latest_model_output_line_num,
            last_malformed_line_num=last_malformed_line_num,
        )

    return await asyncio.to_thread(_read)
