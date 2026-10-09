"""Backlog classification must leave the daemon event loop responsive."""

import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from gobby.hooks import inbox, inbox_envelopes

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_barrier_classifies_two_thousand_envelopes_off_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "hook_type": "session-start",
        "source": "claude",
        "input_data": {"terminal_context": {"gobby_agent_run_id": "retained-run"}},
        "headers": {"X-Gobby-Session-Id": "retained-session"},
    }
    payload = json.dumps(envelope)
    for index in range(2_000):
        (tmp_path / f"{index:04}.json").write_text(payload, encoding="utf-8")

    loop_thread = threading.get_ident()
    original_load = inbox_envelopes.load_envelope
    reads = 0

    def load(path: Path) -> dict[str, Any] | None:
        nonlocal reads
        assert threading.get_ident() != loop_thread, "backlog parsing blocks daemon HTTP"
        reads += 1
        return original_load(path)

    monkeypatch.setattr(inbox_envelopes, "load_envelope", load)
    monkeypatch.setattr(inbox, "_drain_hook_inbox_once_locked", AsyncMock(return_value=0))
    async with asyncio.timeout(5):
        result = await inbox.drain_hook_inbox_barrier(FastAPI(), tmp_path, timeout_seconds=0)

    assert result.timed_out is True
    assert result.residue_hook_count == 2_000
    assert result.unresolved_run_ids == ("retained-run",)
    assert result.unresolved_session_ids == ("retained-session",)
    assert reads >= 2_000
    assert len(list(tmp_path.glob("*.json"))) == 2_000
