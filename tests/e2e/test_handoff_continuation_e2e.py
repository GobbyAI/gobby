"""Real Claude and Codex seats: a daemon-driven handoff compact continues an idle seat.

Execution is a separately admitted operation, like the composer proof: the admission
file and the execution grant must both be present, otherwise the module skips. Once
admitted, every missing prerequisite fails hard. Each case binds a real provider seat
on the isolated daemon, types nothing into the pane, and asserts that the daemon
delivers the get_handoff continuation and that the seat takes and finishes its next
turn within a bound.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

from tests.e2e import test_composer_live_proof as _composer
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_admission import Admission, verify_binary_set
from tests.e2e.composer_proof_live import LiveProof, Seat
from tests.e2e.conftest import daemon_auth_headers

# The admitted isolated-daemon fixture chain is shared with the composer proof.
composer_admission = _composer.composer_admission
composer_fixture = _composer.composer_fixture
e2e_home_dir = _composer.e2e_home_dir

_GRANT = "PD_AUTHORIZED_ISOLATED_EXECUTION"
_BOUND_SECONDS = 240.0
Provider = Literal["claude", "codex"]
_PROVIDERS: tuple[Provider, ...] = ("claude", "codex")
_SCENARIOS = ("compact", "clear", "restart_hold")
_SEAT_PROMPT = "Disposable handoff continuation seat. Reply READY, then wait for the daemon."
_HANDOFF: dict[str, Any] = {
    "current_state": "Disposable continuation seat idle after bind.",
    "next_steps": ["Call get_handoff and reply READY."],
}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GOBBY_COMPOSER_PROOF_EXECUTION") != _GRANT,
        reason="real-provider execution requires separate admission",
    ),
]


def _seat_command(provider: Provider, binary: Path) -> list[str]:
    # MCP stays enabled: the continuation is a get_handoff call the seat itself makes.
    if provider == "claude":
        return [str(binary), _SEAT_PROMPT, "--allowedTools", "mcp__gobby"]
    return [str(binary), "--sandbox", "read-only", "--ask-for-approval", "never", _SEAT_PROMPT]


async def _set_handoff(proof: LiveProof, seat: Seat, *, clear: bool) -> dict[str, Any]:
    """Drive set_handoff as the seat's session; the daemon compacts or clears the idle pane."""
    previous = proof.mcp.session_id
    proof.mcp.session_id = seat.surface.session_id
    try:
        return await proof.call(
            "gobby-sessions", "set_handoff", {**_HANDOFF, "clear_session": clear}
        )
    finally:
        proof.mcp.session_id = previous


def _is_continuation(event: dict[str, Any], seat: Seat, *, clear: bool) -> bool:
    if event.get("type") != "hook_event" or event.get("event_type") != "user-prompt-submit":
        return False
    data = event.get("data") or {}
    if "get_handoff" not in str(data.get("prompt_text", data.get("prompt", ""))):
        return False
    # A cleared session starts under a new external id; a compacted one keeps it.
    if clear:
        return event.get("session_id") != seat.external_id
    return event.get("session_id") == seat.external_id


async def _assert_continuation(
    proof: LiveProof, seat: Seat, *, since: int, writes_before: int, clear: bool
) -> None:
    """The daemon delivers get_handoff, the seat finishes that turn, and nothing was typed."""
    messages = seat.ws.messages
    prompt = await seat.ws.wait_for(
        lambda event: _is_continuation(event, seat, clear=clear) and event in messages[since:],
        timeout=_BOUND_SECONDS,
        description="daemon get_handoff continuation",
    )
    session_id = prompt.get("session_id")
    await seat.ws.wait_for(
        lambda event: event.get("type") == "hook_event"
        and event.get("event_type") == "stop"
        and event.get("session_id") == session_id
        and event in messages[messages.index(prompt) :],
        timeout=_BOUND_SECONDS,
        description="continuation turn finished",
    )
    typed = [
        item for item in proof.trace.events[writes_before:] if item.get("origin") == "operator"
    ]
    if typed:
        raise ProofRefused("the proof typed into the pane; the continuation must be daemon-only")


async def _request_restart(proof: LiveProof) -> httpx.Response:
    """Request a daemon restart; the handoff shutdown hold is prepared before the stop."""
    headers = daemon_auth_headers(proof.daemon.gobby_home)
    url = f"{proof.daemon.http_url}/api/admin/restart"
    return await asyncio.to_thread(httpx.post, url, headers=headers, timeout=_BOUND_SECONDS)


@pytest.mark.parametrize("scenario", _SCENARIOS)
@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_idle_seat_continues_after_daemon_driven_handoff(
    provider: Provider,
    scenario: str,
    composer_fixture: LiveProof,
    composer_admission: Admission,
) -> None:
    verify_binary_set(composer_admission)
    binary = composer_admission.providers[provider].path
    seat = await composer_fixture.bind(provider, _seat_command(provider, binary))
    clear = scenario == "clear"
    since = len(seat.ws.messages)
    writes_before = len(composer_fixture.trace.events)
    restart: asyncio.Task[httpx.Response] | None = None
    if scenario == "restart_hold":
        restart = asyncio.create_task(_request_restart(composer_fixture))
    result = await _set_handoff(composer_fixture, seat, clear=clear)
    if not clear and result.get("compacted") is not True:
        raise ProofRefused(f"{provider} daemon compact not confirmed: {result.get('reason')}")
    await _assert_continuation(
        composer_fixture, seat, since=since, writes_before=writes_before, clear=clear
    )
    if restart is not None:
        response = await restart
        if response.status_code != 200:
            raise ProofRefused(f"restart hold request refused with {response.status_code}")
