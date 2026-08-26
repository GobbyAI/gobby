"""Restart-protected cron run gate behind `gobby stop` / `gobby restart` (#21021)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gobby.cli._daemon_protected_runs import (
    PROTECTED_RUN_POLL_INTERVAL_SECONDS,
    clear_protected_runs,
    describe_protected_run,
    fetch_protected_runs,
)

pytestmark = pytest.mark.unit

RUN: dict[str, Any] = {
    "run_id": "run-1",
    "job_id": "job-1",
    "job_name": "gobby:memory-dream",
    "started_at": "2026-08-26T07:00:00+00:00",
    "elapsed_seconds": 3725.0,
    "remaining_seconds": 12475.0,
}
DESCRIBED = "gobby:memory-dream (running 1h 2m 5s, at most 3h 27m 55s left)"
ENDPOINT = "http://localhost:60887/api/admin/cron/protected-runs"


class _Steps:
    """Collect `_step`-style progress lines, split by their error flag."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.errors: list[str] = []

    def __call__(self, msg: str, *, error: bool = False, scheduled: bool = False) -> None:
        (self.errors if error else self.messages).append(msg)


def _raise_connect_error() -> MagicMock:
    raise httpx.ConnectError("refused")


def _response(status_code: int, payload: object = None, *, bad_json: bool = False) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    if bad_json:
        response.json.side_effect = ValueError("bad json")
    else:
        response.json.return_value = payload
    return response


def test_fetch_protected_runs_returns_the_daemons_run_rows() -> None:
    with patch(
        "gobby.cli._daemon_protected_runs.httpx.get",
        return_value=_response(200, {"runs": [RUN, "junk"]}),
    ) as get:
        assert fetch_protected_runs(60887) == [RUN]

    get.assert_called_once_with(ENDPOINT, timeout=3.0)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_raise_connect_error, id="unreachable"),
        pytest.param(lambda: _response(503, {"detail": "runner unavailable"}), id="starting"),
        pytest.param(lambda: _response(200, bad_json=True), id="bad-json"),
        pytest.param(lambda: _response(200, {"status": "ok"}), id="no-runs-key"),
        pytest.param(lambda: _response(200, ["not", "a", "dict"]), id="wrong-shape"),
    ],
)
def test_fetch_protected_runs_treats_an_unanswerable_daemon_as_no_lease(
    outcome: Callable[[], MagicMock],
) -> None:
    with patch("gobby.cli._daemon_protected_runs.httpx.get", side_effect=lambda *a, **k: outcome()):
        assert fetch_protected_runs(60887) == []


def test_describe_protected_run_names_the_job_and_both_durations() -> None:
    assert describe_protected_run(RUN) == DESCRIBED
    assert describe_protected_run({"run_id": "run-9"}) == "run-9 (running 0s, at most 0s left)"


def test_clear_protected_runs_proceeds_when_nothing_holds_the_lease() -> None:
    steps = _Steps()

    assert clear_protected_runs(1, force=False, wait=False, step=steps, fetch=lambda _: []) is True

    assert steps.messages == []
    assert steps.errors == []


def test_clear_protected_runs_refuses_by_default() -> None:
    steps = _Steps()

    proceed = clear_protected_runs(1, force=False, wait=False, step=steps, fetch=lambda _: [RUN])

    assert proceed is False
    assert steps.messages == []
    assert steps.errors[0] == f"Protected cron run active: {DESCRIBED}"
    assert "--wait" in steps.errors[1]
    assert "--force" in steps.errors[1]


def test_clear_protected_runs_force_names_what_it_interrupts() -> None:
    steps = _Steps()

    proceed = clear_protected_runs(1, force=True, wait=False, step=steps, fetch=lambda _: [RUN])

    assert proceed is True
    assert steps.errors == []
    assert steps.messages == [
        f"Interrupting protected cron run {DESCRIBED}; it resumes after the next start"
    ]


def test_clear_protected_runs_wait_polls_until_the_lease_clears() -> None:
    steps = _Steps()
    fetch = MagicMock(side_effect=[[RUN], [RUN], []])

    with patch("gobby.cli._daemon_protected_runs.time.sleep") as sleep:
        proceed = clear_protected_runs(1, force=False, wait=True, step=steps, fetch=fetch)

    assert proceed is True
    assert fetch.call_count == 3
    assert sleep.call_args_list == [((PROTECTED_RUN_POLL_INTERVAL_SECONDS,),)] * 2
    assert steps.errors == []
    assert steps.messages == [
        f"Waiting for protected cron run(s) to finish: {DESCRIBED}",
        "Protected cron run(s) finished",
    ]


def test_clear_protected_runs_wait_gives_up_past_the_runs_own_timeout() -> None:
    steps = _Steps()
    fetch = MagicMock(return_value=[RUN])

    with (
        patch("gobby.cli._daemon_protected_runs.time.sleep") as sleep,
        patch("gobby.cli._daemon_protected_runs.time.monotonic", side_effect=[0.0, 0.0, 1e9]),
    ):
        proceed = clear_protected_runs(1, force=False, wait=True, step=steps, fetch=fetch)

    assert proceed is False
    assert fetch.call_count == 2
    assert sleep.call_count == 1
    assert steps.errors == [
        "Protected cron run(s) still active past their own timeout; refusing to stop"
    ]
