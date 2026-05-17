"""Unit coverage for the web-facing build_state derivation (plan D3)."""

from __future__ import annotations

import pytest

from gobby.build.lifecycle import derive_build_state

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("allow_automation", "has_build_event", "expected"),
    [
        # Never opted into automation and never built.
        (False, False, "never_started"),
        # Automation currently enabled — running regardless of history.
        (True, False, "running"),
        (True, True, "running"),
        # The build_stop_target path: stop clears allow_automation WITHOUT
        # recording a new lifecycle event or bumping dispatch_failure_count,
        # so the durable build event is the only honest signal — paused.
        (False, True, "paused"),
    ],
)
def test_derive_build_state(
    allow_automation: bool, has_build_event: bool, expected: str
) -> None:
    assert (
        derive_build_state(
            allow_automation=allow_automation,
            has_build_event=has_build_event,
        )
        == expected
    )


def test_running_takes_precedence_over_stale_history() -> None:
    """A resumed build re-enables allow_automation; even with prior build
    history present it must read as running, never paused."""
    assert (
        derive_build_state(allow_automation=True, has_build_event=True) == "running"
    )
