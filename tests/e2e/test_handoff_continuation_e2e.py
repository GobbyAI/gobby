"""Real Claude and Codex seats: a seat's own set_handoff continues it through the daemon.

Execution is a separately admitted operation, like the composer proof: the admission
file and the execution grant must both be present, otherwise the module skips. Once
admitted, every missing prerequisite fails hard. Each case binds a real provider seat
on the isolated daemon. The seat loads the handoff reference, fetches the set_handoff
schema, and calls set_handoff in its launch turn. The terminal result stages the
handoff, and the daemon delivers it. The test types nothing into the pane. It asserts
that the daemon delivers the get_handoff continuation and that the seat finishes that
turn within a bound.

The restart_hold scenario requests a daemon restart after the launch turn has ended
with the handoff staged. The handoff shutdown hold refuses it with 409 handoff_pending.
The staged handoff stays live until the continuation's get_handoff consumes it. This
test does not admit a restart after consumption, because an admitted restart stops the
isolated daemon that the case still tears down.
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

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GOBBY_COMPOSER_PROOF_EXECUTION") != _GRANT,
        reason="real-provider execution requires separate admission",
    ),
]


def _seat_prompt(*, clear: bool) -> str:
    # The launch turn stages the seat's own handoff. The prompt must not name the
    # continuation tool, or the launch turn itself would match the continuation filter.
    return (
        "Disposable handoff continuation seat. Load the gobby:references/sessions/handoffs.md "
        "skill reference with gobby-skills get_skill. Fetch the gobby-sessions set_handoff "
        "schema with get_tool_schema. Then call set_handoff with "
        f"clear_session={str(clear).lower()}, current_state "
        "'Disposable continuation seat idle after bind.', and next_steps ['Reply READY.']. "
        "Reply READY after the call, then stop."
    )


def _seat_command(provider: Provider, binary: Path, *, clear: bool) -> list[str]:
    # MCP stays enabled: the seat itself loads the reference and calls set_handoff.
    prompt = _seat_prompt(clear=clear)
    if provider == "claude":
        return [str(binary), prompt, "--allowedTools", "mcp__gobby"]
    return [str(binary), "--sandbox", "read-only", "--ask-for-approval", "never", prompt]


def _is_turn_end(event: dict[str, Any], seat: Seat) -> bool:
    return (
        event.get("type") == "hook_event"
        and event.get("event_type") == "stop"
        and event.get("session_id") == seat.external_id
    )


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


async def _request_restart(proof: LiveProof) -> httpx.Response:
    """Request a graceful restart without force, so the handoff shutdown hold applies."""
    headers = daemon_auth_headers(proof.daemon.gobby_home)
    url = f"{proof.daemon.http_url}/api/admin/restart"
    return await asyncio.to_thread(httpx.post, url, headers=headers, timeout=_BOUND_SECONDS)


async def _assert_continuation(
    proof: LiveProof, seat: Seat, *, writes_before: int, clear: bool
) -> None:
    """The daemon delivers get_handoff, the seat finishes that turn, and nothing was typed."""
    prompt = await seat.ws.wait_for(
        lambda event: _is_continuation(event, seat, clear=clear),
        timeout=_BOUND_SECONDS,
        description="daemon get_handoff continuation",
    )
    # Messages arrive in order. The launch turn's stop precedes the prompt, so only a
    # stop in the continuation's own session after the prompt ends the continuation turn.
    prompt_seen = False

    def turn_finished(event: dict[str, Any]) -> bool:
        nonlocal prompt_seen
        if event == prompt:
            prompt_seen = True
            return False
        return (
            prompt_seen
            and event.get("type") == "hook_event"
            and event.get("event_type") == "stop"
            and event.get("session_id") == prompt.get("session_id")
        )

    await seat.ws.wait_for(
        turn_finished, timeout=_BOUND_SECONDS, description="continuation turn finished"
    )
    typed = [
        item for item in proof.trace.events[writes_before:] if item.get("origin") == "operator"
    ]
    if typed:
        raise ProofRefused("the proof typed into the pane; the continuation must be daemon-only")


@pytest.mark.parametrize("scenario", _SCENARIOS)
@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_seat_continues_after_its_own_handoff(
    provider: Provider,
    scenario: str,
    composer_fixture: LiveProof,
    composer_admission: Admission,
) -> None:
    verify_binary_set(composer_admission)
    binary = composer_admission.providers[provider].path
    clear = scenario == "clear"
    seat = await composer_fixture.bind(provider, _seat_command(provider, binary, clear=clear))
    writes_before = len(composer_fixture.trace.events)
    # The launch turn ends only after set_handoff returns, so the handoff is staged by then.
    await seat.ws.wait_for(
        lambda event: _is_turn_end(event, seat),
        timeout=_BOUND_SECONDS,
        description="launch turn ended after the seat's set_handoff",
    )
    if scenario == "restart_hold":
        held = await _request_restart(composer_fixture)
        assert held.status_code == 409, held.text
        assert held.json().get("status") == "handoff_pending", held.text
    await _assert_continuation(composer_fixture, seat, writes_before=writes_before, clear=clear)
