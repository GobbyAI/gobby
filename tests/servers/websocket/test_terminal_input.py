"""Mediated terminal input observation stays off the WebSocket event loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gobby.servers.websocket.terminal_input import record_turn_observation


@pytest.mark.asyncio
async def test_mediated_turn_observation_runs_blocking_lookup_off_loop() -> None:
    observed: list[tuple[int, str, str, str, int | None]] = []

    def record(
        terminal_id: str, payload: str, outcome: str, *, input_seq: int | None = None
    ) -> None:
        observed.append((threading.get_ident(), terminal_id, payload, outcome, input_seq))

    async def record_async(
        terminal_id: str, payload: str, outcome: str, *, input_seq: int | None = None
    ) -> None:
        await asyncio.to_thread(record, terminal_id, payload, outcome, input_seq=input_seq)

    owner = SimpleNamespace(
        terminal_turn_observer=SimpleNamespace(record_mediated_input_async=record_async)
    )
    loop_thread = threading.get_ident()

    await record_turn_observation(
        owner, "terminal-1", kind="input", payload="\x03", outcome="delivered", seq=7
    )

    assert len(observed) == 1
    assert observed[0][1:] == ("terminal-1", "\x03", "delivered", 7)
    assert observed[0][0] != loop_thread
