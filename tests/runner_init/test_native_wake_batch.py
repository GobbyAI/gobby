"""The native wake batch drains only composers no probe confirmed empty."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from gobby.events.wake import CONTINUE_WAKE_MESSAGE, NativeWakeTarget
from gobby.runner_init import orchestration
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.write_coordinator import NativeWakeBatchRequest

pytestmark = pytest.mark.unit


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.requests: list[NativeWakeBatchRequest] = []

    async def run_native_wake_batch(
        self, requests: Sequence[NativeWakeBatchRequest]
    ) -> list[object]:
        self.requests.extend(requests)
        return []


async def test_confirmed_empty_target_gets_no_drain_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _RecordingCoordinator()
    monkeypatch.setattr(orchestration, "wake_write_services", lambda: (None, coordinator))

    await orchestration._send_native_wake_batch(
        [
            NativeWakeTarget("s-empty", "t-empty", "claude", drain=False),
            NativeWakeTarget("s-blind", "t-blind", "claude"),
        ]
    )

    empty, blind = coordinator.requests
    assert [(op.kind, op.payload) for op in empty.operations] == [
        ("text", CONTINUE_WAKE_MESSAGE),
        ("key", "enter"),
    ]
    drain = [("key", key) for key in composer_clear_sequence("claude")]
    assert [(op.kind, op.payload) for op in blind.operations] == [
        *drain,
        ("text", CONTINUE_WAKE_MESSAGE),
        ("key", "enter"),
    ]
