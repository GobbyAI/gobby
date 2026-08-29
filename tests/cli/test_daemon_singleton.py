"""Operator-facing wording for singleton probe states."""

from __future__ import annotations

import pytest

from gobby.cli.daemon_singleton import format_singleton_status
from gobby.runner_pid_file import ProbeState, SingletonProbe

pytestmark = pytest.mark.unit


def test_live_reservation_names_the_start_in_progress_and_the_holder_pid() -> None:
    status = format_singleton_status(SingletonProbe(state=ProbeState.LIVE_RESERVATION, pid=4242))

    assert (
        status == "Gobby daemon: starting (PID: 4242); a start reservation is live, retry shortly"
    )


def test_live_reservation_without_a_pid_still_names_the_start_in_progress() -> None:
    status = format_singleton_status(SingletonProbe(state=ProbeState.LIVE_RESERVATION))

    assert status == "Gobby daemon: starting; a start reservation is live, retry shortly"
    assert "PID" not in status


def test_stale_reservation_reports_a_start_that_never_finished() -> None:
    status = format_singleton_status(SingletonProbe(state=ProbeState.STALE_RESERVATION, pid=4242))

    assert status == (
        "Gobby daemon: not running; an earlier start did not finish, run `gobby start`"
    )


@pytest.mark.parametrize(
    "state",
    [ProbeState.LIVE_RESERVATION, ProbeState.STALE_RESERVATION],
)
def test_reservation_states_never_render_the_raw_probe_state_name(state: ProbeState) -> None:
    status = format_singleton_status(SingletonProbe(state=state, pid=4242))

    assert status != f"Gobby singleton: {state.value.replace('_', ' ')}"
    assert state.value not in status
    assert state.value.replace("_", " ") not in status
