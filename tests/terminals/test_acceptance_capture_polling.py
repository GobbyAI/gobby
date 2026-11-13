"""Acceptance screen waits tolerate captures invalidated by concurrent output."""

from uuid import UUID

import pytest

from gobby.terminals.host_client import HostCommandError
from tests.terminals.acceptance import conftest as acceptance
from tests.terminals.fakes import FakeRuntime, make_memory_terminal


@pytest.mark.parametrize("error", ["capture_busy", "capture_invalidated"])
@pytest.mark.parametrize("probe", ["text", "size"])
async def test_screen_wait_retries_transient_capture(
    monkeypatch: pytest.MonkeyPatch, error: str, probe: str
) -> None:
    token = UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(acceptance, "uuid4", lambda: token)
    screen = f"40 120-SIZE-{token.hex[:12]}"
    runtime = FakeRuntime(snapshot_effects=[HostCommandError(error), screen])
    live = acceptance.LiveTerminal("tmux", runtime, make_memory_terminal())

    if probe == "text":
        assert await acceptance.wait_for_text(live, screen, description="coherent screen") == screen
    else:
        assert await acceptance.observed_size(live) == (40, 120)
    assert runtime.snapshot_effects == []
    assert len(runtime.snapshot_modes) == 2


@pytest.mark.parametrize("probe", ["text", "size"])
async def test_screen_wait_propagates_other_capture_errors(probe: str) -> None:
    runtime = FakeRuntime(snapshot_effects=[HostCommandError("terminal_not_found")])
    live = acceptance.LiveTerminal("tmux", runtime, make_memory_terminal())

    with pytest.raises(HostCommandError, match="terminal_not_found"):
        if probe == "text":
            await acceptance.wait_for_text(live, "ready", description="coherent screen")
        else:
            await acceptance.observed_size(live)
    assert len(runtime.snapshot_modes) == 1
