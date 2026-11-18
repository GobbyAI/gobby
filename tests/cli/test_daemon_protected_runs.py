"""Restart-protected cron run gate behind `gobby stop` / `gobby restart` (#21021)."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import _daemon_protected_runs as protected_runs
from gobby.cli import cli
from gobby.cli._daemon_protected_runs import (
    PROTECTED_RUN_MAX_WAIT_SECONDS,
    PROTECTED_RUN_POLL_INTERVAL_SECONDS,
    clear_protected_runs,
    describe_protected_run,
    fetch_protected_runs,
    fetch_signed_protected_runs,
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
AUTH_HEADERS = {"Authorization": "Bearer test-local-cli-token"}


@pytest.fixture(autouse=True)
def _fake_local_token() -> Generator[None]:
    """Keep the probe off the real startup bootstrap API key."""
    with patch(
        "gobby.cli._daemon_protected_runs.daemon_auth_headers",
        return_value=dict(AUTH_HEADERS),
    ):
        yield


class _Steps:
    """Collect `_step`-style progress lines, split by their error flag."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.errors: list[str] = []

    def __call__(self, msg: str, *, error: bool = False, scheduled: bool = False) -> None:
        (self.errors if error else self.messages).append(msg)


def _response(status_code: int, payload: object = None, *, bad_json: bool = False) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    if bad_json:
        response.json.side_effect = ValueError("bad json")
    else:
        response.json.return_value = payload
    return response


def test_fetch_protected_runs_authenticates_and_returns_the_daemons_run_rows() -> None:
    """The admin API is token-gated; an unauthenticated probe would 401 into 'no lease'."""
    with patch(
        "gobby.cli._daemon_protected_runs.httpx.get",
        return_value=_response(200, {"runs": [RUN, "junk"]}),
    ) as get:
        assert fetch_protected_runs(60887) == [RUN]

    get.assert_called_once_with(ENDPOINT, headers=AUTH_HEADERS, timeout=3.0)
    sent_headers = get.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer test-local-cli-token"


def _unknown_answer_error() -> type[Exception]:
    """The unknown-answer exception, looked up when a test runs so this module still imports."""
    unknown: type[Exception] = vars(protected_runs)["ProtectedRunsUnknown"]
    return unknown


UNUSABLE_ANSWERS = [
    pytest.param(lambda: _response(503, {"detail": "runner unavailable"}), id="starting"),
    pytest.param(lambda: _response(401, {"detail": "unauthorized"}), id="unauthorized"),
    pytest.param(lambda: _response(200, bad_json=True), id="bad-json"),
    pytest.param(lambda: _response(200, {"status": "ok"}), id="no-runs-key"),
    pytest.param(lambda: _response(200, ["not", "a", "dict"]), id="wrong-shape"),
    pytest.param(lambda: _response(200, {"runs": "not-a-list"}), id="runs-not-a-list"),
]
SIGNED_HEADERS = {"X-Gobby-Signature": "fixture-signature"}


def test_fetch_protected_runs_treats_an_unreachable_daemon_as_no_lease() -> None:
    with patch(
        "gobby.cli._daemon_protected_runs.httpx.get",
        side_effect=httpx.ConnectError("refused"),
    ):
        assert fetch_protected_runs(60887) == []


@pytest.mark.parametrize("outcome", UNUSABLE_ANSWERS)
def test_fetch_protected_runs_raises_unknown_when_the_daemon_answers_unusably(
    outcome: Callable[[], MagicMock],
) -> None:
    with (
        patch("gobby.cli._daemon_protected_runs.httpx.get", side_effect=lambda *a, **k: outcome()),
        pytest.raises(_unknown_answer_error()),
    ):
        fetch_protected_runs(60887)


def test_fetch_protected_runs_reports_a_known_empty_answer_as_no_runs() -> None:
    with patch(
        "gobby.cli._daemon_protected_runs.httpx.get",
        return_value=_response(200, {"runs": []}),
    ):
        assert fetch_protected_runs(60887) == []


def test_fetch_signed_protected_runs_sends_the_signed_request_and_returns_the_runs() -> None:
    with (
        patch(
            "gobby.cli._daemon_protected_runs.signed_daemon_headers",
            return_value=dict(SIGNED_HEADERS),
        ),
        patch(
            "gobby.cli._daemon_protected_runs.httpx.get",
            return_value=_response(200, {"runs": [RUN]}),
        ) as get,
    ):
        assert fetch_signed_protected_runs(60887) == [RUN]

    get.assert_called_once_with(ENDPOINT, headers=SIGNED_HEADERS, timeout=3.0)


def test_fetch_signed_protected_runs_treats_an_unreachable_daemon_as_no_lease() -> None:
    with (
        patch(
            "gobby.cli._daemon_protected_runs.signed_daemon_headers",
            return_value=dict(SIGNED_HEADERS),
        ),
        patch(
            "gobby.cli._daemon_protected_runs.httpx.get",
            side_effect=httpx.ConnectError("refused"),
        ),
    ):
        assert fetch_signed_protected_runs(60887) == []


def test_fetch_signed_protected_runs_reports_no_runs_when_the_seat_cannot_sign() -> None:
    with patch("gobby.cli._daemon_protected_runs.signed_daemon_headers", return_value=None):
        assert fetch_signed_protected_runs(60887) == []


@pytest.mark.parametrize("outcome", UNUSABLE_ANSWERS)
def test_fetch_signed_protected_runs_raises_unknown_when_the_daemon_answers_unusably(
    outcome: Callable[[], MagicMock],
) -> None:
    with (
        patch(
            "gobby.cli._daemon_protected_runs.signed_daemon_headers",
            return_value=dict(SIGNED_HEADERS),
        ),
        patch("gobby.cli._daemon_protected_runs.httpx.get", side_effect=lambda *a, **k: outcome()),
        pytest.raises(_unknown_answer_error()),
    ):
        fetch_signed_protected_runs(60887)


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


def test_clear_protected_runs_wait_is_bounded_and_names_the_run_on_expiry() -> None:
    """A dream with hours left must not defer the stop for hours (restart 1, 2026-10-04)."""
    steps = _Steps()
    fetch = MagicMock(return_value=[RUN])
    past_cap = PROTECTED_RUN_MAX_WAIT_SECONDS + 1.0

    with (
        patch("gobby.cli._daemon_protected_runs.time.sleep") as sleep,
        patch(
            "gobby.cli._daemon_protected_runs.time.monotonic",
            side_effect=[0.0, 0.0, past_cap],
        ),
    ):
        proceed = clear_protected_runs(1, force=False, wait=True, step=steps, fetch=fetch)

    assert proceed is False
    assert fetch.call_count == 2
    assert sleep.call_count == 1
    assert steps.errors == [
        f"Protected cron run still active: {DESCRIBED}",
        "Refusing to stop the daemon after waiting 10m; "
        "re-run with --wait later or --force to interrupt it now",
    ]


def test_clear_protected_runs_wait_refuses_at_the_exact_deadline_without_another_poll() -> None:
    """Reaching the cap exactly refuses at once instead of sleeping one more interval."""
    steps = _Steps()
    fetch = MagicMock(return_value=[RUN])

    with (
        patch("gobby.cli._daemon_protected_runs.time.sleep") as sleep,
        patch(
            "gobby.cli._daemon_protected_runs.time.monotonic",
            side_effect=[0.0, PROTECTED_RUN_MAX_WAIT_SECONDS, PROTECTED_RUN_MAX_WAIT_SECONDS + 1.0],
        ),
    ):
        proceed = clear_protected_runs(1, force=False, wait=True, step=steps, fetch=fetch)

    assert proceed is False
    sleep.assert_not_called()
    assert fetch.call_count == 1
    assert steps.errors[-1].startswith("Refusing to stop the daemon after waiting 10m")


def test_clear_protected_runs_wait_gives_up_past_a_shorter_own_timeout() -> None:
    """A run whose own timeout is inside the cap bounds the wait, and the last poll is clipped."""
    steps = _Steps()
    short = {**RUN, "remaining_seconds": 30.0}
    fetch = MagicMock(return_value=[short])
    own_deadline = 30.0 + PROTECTED_RUN_POLL_INTERVAL_SECONDS

    with (
        patch("gobby.cli._daemon_protected_runs.time.sleep") as sleep,
        patch(
            "gobby.cli._daemon_protected_runs.time.monotonic",
            side_effect=[0.0, own_deadline - 5.0, own_deadline, own_deadline + 1.0],
        ),
    ):
        proceed = clear_protected_runs(1, force=False, wait=True, step=steps, fetch=fetch)

    assert proceed is False
    assert sleep.call_args_list == [((5.0,),)]
    assert fetch.call_count == 2
    assert steps.errors[-1] == (
        "Refusing to stop the daemon after waiting 45s; "
        "re-run with --wait later or --force to interrupt it now"
    )


def _fetch_unknown(_http_port: int) -> list[dict[str, Any]]:
    raise _unknown_answer_error()("the daemon answered 503")


def test_clear_protected_runs_reports_an_unknown_answer_and_refuses_without_force() -> None:
    steps = _Steps()

    proceed = clear_protected_runs(1, force=False, wait=False, step=steps, fetch=_fetch_unknown)

    assert proceed is False
    assert any("unknown" in message for message in steps.errors)


def test_clear_protected_runs_unknown_answer_with_wait_refuses_without_sleeping() -> None:
    steps = _Steps()

    with patch("gobby.cli._daemon_protected_runs.time.sleep") as sleep:
        proceed = clear_protected_runs(1, force=False, wait=True, step=steps, fetch=_fetch_unknown)

    assert proceed is False
    sleep.assert_not_called()
    assert any("unknown" in message for message in steps.errors)


def test_clear_protected_runs_unknown_answer_with_force_proceeds_and_says_so() -> None:
    steps = _Steps()

    proceed = clear_protected_runs(1, force=True, wait=False, step=steps, fetch=_fetch_unknown)

    assert proceed is True
    assert any("without knowing" in message for message in steps.messages + steps.errors)


@patch("gobby.cli.daemon_lifecycle._services_stop")
@patch("gobby.cli.daemon_lifecycle.stop_daemon_util")
@patch("gobby.cli.daemon_lifecycle.service_stop")
@patch("gobby.cli.daemon_lifecycle.get_service_status")
@patch("gobby.cli.daemon_lifecycle.fetch_protected_runs", return_value=[RUN])
@patch("gobby.cli.daemon_lifecycle.stop_singleton_gate", return_value=("continue", None))
@patch("gobby.cli.runtime.CliRuntime.require_config")
@patch.dict(os.environ, {"GOBBY_TEST_PROTECT": ""})
def test_stop_wait_refused_on_expiry_stops_nothing(
    mock_load_config: MagicMock,
    mock_gate: MagicMock,
    mock_fetch: MagicMock,
    mock_get_service_status: MagicMock,
    mock_service_stop: MagicMock,
    mock_stop_daemon: MagicMock,
    mock_services_stop: MagicMock,
) -> None:
    """The bounded wait refuses before any service, signal, or Docker stop."""
    mock_load_config.return_value = MagicMock(daemon_port=60887)

    with (
        patch("gobby.cli._daemon_protected_runs.time.sleep"),
        patch(
            "gobby.cli._daemon_protected_runs.time.monotonic",
            side_effect=[0.0, PROTECTED_RUN_MAX_WAIT_SECONDS + 1.0],
        ),
    ):
        result = CliRunner().invoke(cli, ["stop", "--docker", "--wait"])

    assert result.exit_code == 1
    assert f"Protected cron run still active: {DESCRIBED}" in result.output
    assert "Refusing to stop the daemon after waiting 10m" in result.output
    mock_fetch.assert_called_once_with(60887)
    mock_get_service_status.assert_not_called()
    mock_service_stop.assert_not_called()
    mock_stop_daemon.assert_not_called()
    mock_services_stop.assert_not_called()
