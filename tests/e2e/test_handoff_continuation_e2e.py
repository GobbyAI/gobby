"""Real fleet seats: a seat's own set_handoff continues it through the daemon.

Execution is a separately admitted operation, like the composer proof: the admission
file and the execution grant must both be present, otherwise the module skips. Once
admitted, every missing prerequisite fails hard. Each case binds a real provider seat
on the isolated daemon. The seat loads the handoff reference, fetches the set_handoff
schema, and calls set_handoff in its launch turn. The terminal result stages the
handoff, and the daemon delivers it. The test types nothing into the pane. It asserts
that the daemon delivers the get_handoff continuation and that the seat finishes that
turn within a bound.

The restart_hold scenario uses a separate test-only runner to hold compact submission.
The provider's launch turn ends normally. While compact is held, the test requests restart,
asserts 409 handoff_pending and the live marker, then releases compact. The barrier prevents
continuation consumption throughout the restart judgment, including its start probe.
No production seam, provider sleep or typed input is used.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

from gobby.agents.trust import pre_approve_directory
from tests.e2e import handoff_continuation_fixture as _handoff
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_live import LiveProof, Seat
from tests.e2e.conftest import daemon_auth_headers
from tests.e2e.handoff_continuation_admission import Admission, verify_binary_set
from tests.e2e.handoff_continuation_barrier import HandoffBarrier
from tests.e2e.handoff_continuation_launch import provider_launch
from tests.e2e.handoff_continuation_runner import adopt

handoff_admission = _handoff.handoff_admission
provider_environment = _handoff.provider_environment
handoff_fixture = _handoff.handoff_fixture

_GRANT = "PD_AUTHORIZED_ISOLATED_EXECUTION"
_BOUND_SECONDS = 240.0
Provider = Literal["claude", "codex", "grok", "agy", "droid"]
_PROVIDERS: tuple[Provider, ...] = ("claude", "codex", "grok", "agy", "droid")
_SCENARIOS = ("compact", "clear", "restart_hold")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GOBBY_HANDOFF_PROOF_EXECUTION") != _GRANT,
        reason="real-provider execution requires separate admission",
    ),
]


_REPLY_TAIL = "Reply READY after the call, then stop."


def _seat_prompt(scenario: str) -> str:
    # The launch turn stages the seat's own handoff. The prompt must not name the
    # continuation tool, or the launch turn itself would match the continuation filter.
    clear = str(scenario == "clear").lower()
    return (
        "Disposable handoff continuation seat. Load the gobby:references/sessions/handoffs.md "
        "skill reference with gobby-skills get_skill. Fetch the gobby-sessions set_handoff "
        "schema with get_tool_schema. Then call set_handoff with "
        f"clear_session={clear}, current_state "
        "'Disposable continuation seat idle after bind.', and next_steps ['Reply READY.']. "
        f"{_REPLY_TAIL}"
    )


# The gobby proxy tools the seat's handoff path calls. get_skill, set_handoff and the daemon's
# get_handoff continuation all route through call_tool; the schema fetch uses get_tool_schema.
_GOBBY_PROXY_TOOLS = ("mcp__gobby__call_tool", "mcp__gobby__get_tool_schema")


def _seat_command(provider: Provider, binary: Path, scenario: str) -> list[str]:
    # Fixture-only exception to the composer's disabled tools: the seat may call the gobby proxy.
    prompt = _seat_prompt(scenario)
    if provider == "claude":
        allowed = list(_GOBBY_PROXY_TOOLS)
        return [str(binary), prompt, "--tools", "", "--allowedTools", *allowed]
    if provider == "grok":
        return [str(binary), "--always-approve", "--no-alt-screen", prompt]
    if provider == "agy":
        return [str(binary), "--prompt-interactive", prompt]
    if provider == "droid":
        return [str(binary), prompt]
    return [
        str(binary),
        "-c",
        "mcp_servers.gobby.enabled=true",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        prompt,
    ]


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
    if "get_handoff" not in str(data.get("prompt_text", "")):
        return False
    # A cleared session starts under a new external id; a compacted one keeps it.
    if clear:
        return event.get("session_id") != seat.external_id
    return event.get("session_id") == seat.external_id


def _compact_boundary_recorded(db: Any, seat: Seat) -> bool:
    """Whether the daemon recorded this seat's compact boundary.

    The row is inserted by notify_compact_boundary, which runs before the continuation
    prompt is scheduled, so the continuation prompt alone cannot satisfy this check.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM session_handoff_deliveries"
            " WHERE continuation_session_id = %s AND boundary_kind = 'compact'",
            (seat.surface.session_id,),
        ).fetchone()
    return row is not None


def _handoff_staged(db: Any) -> bool:
    """Whether a set_handoff marker is live on the hub.

    The shutdown guard refuses a restart while any such marker exists. The marker is cleared
    only when get_handoff consumes it, and that call follows the compact, so a live marker
    means the restart is held.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM session_variables"
            " WHERE jsonb_typeof(variables -> 'set_handoff_pending') = 'object'"
            " LIMIT 1"
        ).fetchone()
    return row is not None


async def _request_restart(proof: LiveProof) -> httpx.Response:
    """Request a graceful restart without force, so the handoff shutdown hold applies."""
    headers = daemon_auth_headers(proof.daemon.gobby_home)
    url = f"{proof.daemon.http_url}/api/admin/restart"
    return await asyncio.to_thread(httpx.post, url, headers=headers, timeout=_BOUND_SECONDS)


async def _assert_restart_held(
    proof: LiveProof, seat: Seat, barrier: HandoffBarrier, db: Any
) -> None:
    await barrier.wait_for(seat.surface.session_id)
    held = await _request_restart(proof)
    assert held.status_code == 409, held.text
    assert held.json().get("status") == "handoff_pending", held.text
    assert await asyncio.to_thread(_handoff_staged, db), (
        "the staged handoff was consumed while compact submission was held"
    )
    barrier.release()


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
    handoff_fixture: tuple[LiveProof, HandoffBarrier],
    handoff_admission: Admission,
    provider_environment: dict[str, str],
    postgres_db: Any,
    e2e_project_dir: Path,
) -> None:
    composer_fixture, barrier = handoff_fixture
    verify_binary_set(handoff_admission)
    binary = handoff_admission.providers[provider].path
    clear = scenario == "clear"
    # A spawned seat starts in a pre-trusted folder; without it the interactive CLI
    # waits at its folder-trust dialog and never fires SessionStart.
    pre_approve_directory(provider, e2e_project_dir)
    command = provider_launch(
        _seat_command(provider, binary, scenario), provider_environment, provider=provider
    )
    try:
        seat = await adopt(composer_fixture, provider, command)
    except (TimeoutError, ProofRefused) as exc:
        raise ProofRefused(
            f"{provider}: real seat failed registration; verify normal login and native hooks"
        ) from exc
    writes_before = len(composer_fixture.trace.events)
    await seat.ws.wait_for(
        lambda event: event.get("type") == "hook_event"
        and event.get("event_type") == "session-start"
        and event.get("session_id") == seat.external_id,
        timeout=_BOUND_SECONDS,
        description=f"{provider}: native SessionStart on the isolated daemon",
    )
    composer_fixture.evidence.append(
        {
            "kind": "isolated-hook-registration",
            "provider": provider,
            "session_id": seat.surface.session_id,
            "external_id": seat.external_id,
            "daemon_url": provider_environment["GOBBY_DAEMON_URL"],
        }
    )
    if scenario == "restart_hold":
        await _assert_restart_held(composer_fixture, seat, barrier, postgres_db)
    # The launch turn ends only after set_handoff returns, so the handoff is staged by then.
    await seat.ws.wait_for(
        lambda event: _is_turn_end(event, seat),
        timeout=_BOUND_SECONDS,
        description=f"{provider}: launch turn after set_handoff (normal login required)",
    )
    await _assert_continuation(composer_fixture, seat, writes_before=writes_before, clear=clear)
    # A clear is not a compaction, so its boundary kind is not asserted here.
    if scenario != "clear":
        assert await asyncio.to_thread(_compact_boundary_recorded, postgres_db, seat), (
            "the daemon recorded no compact boundary for this seat"
        )
    composer_fixture.matrix_passed = True
