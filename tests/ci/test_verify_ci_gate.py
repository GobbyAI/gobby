from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from scripts.ci import verify_ci_gate

pytestmark = pytest.mark.unit


def test_fetch_workflow_runs_requests_required_fields() -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> str:
        calls.append(list(command))
        return "[]"

    assert (
        verify_ci_gate.fetch_workflow_runs(
            repo="GobbyAI/gobby",
            workflow="CI",
            sha="abc123",
            runner=runner,
        )
        == []
    )

    assert calls == [
        [
            "gh",
            "run",
            "list",
            "--repo",
            "GobbyAI/gobby",
            "--workflow",
            "CI",
            "--commit",
            "abc123",
            "--limit",
            "50",
            "--json",
            "createdAt,databaseId,status,conclusion,event,headSha,url",
        ]
    ]


def test_latest_successful_manual_rerun_passes_after_older_cancelled_run() -> None:
    runs = verify_ci_gate.parse_workflow_runs(
        _runs_json(
            _run(
                created_at="2026-05-18T10:00:00Z",
                database_id=1,
                conclusion="cancelled",
                event="push",
            ),
            _run(
                created_at="2026-05-18T10:05:00Z",
                database_id=2,
                conclusion="success",
                event="workflow_dispatch",
            ),
        )
    )

    decision = verify_ci_gate.evaluate_runs(runs, "abc123")

    assert decision.state == "pass"
    assert decision.run is not None
    assert decision.run.database_id == 2


def test_latest_considered_failed_run_is_authoritative() -> None:
    runs = verify_ci_gate.parse_workflow_runs(
        _runs_json(
            _run(
                created_at="2026-05-18T10:00:00Z",
                database_id=1,
                conclusion="success",
                event="push",
            ),
            _run(
                created_at="2026-05-18T10:05:00Z",
                database_id=2,
                conclusion="failure",
                event="workflow_dispatch",
            ),
        )
    )

    decision = verify_ci_gate.evaluate_runs(runs, "abc123")

    assert decision.state == "fail"
    assert decision.run is not None
    assert decision.run.database_id == 2


def test_pull_request_runs_are_ignored_until_poll_bound() -> None:
    def runner(_command: Sequence[str]) -> str:
        return _runs_json(
            _run(
                created_at="2026-05-18T10:00:00Z",
                database_id=1,
                conclusion="success",
                event="pull_request",
            )
        )

    assert (
        verify_ci_gate.wait_for_ci_gate(
            repo="GobbyAI/gobby",
            workflow="CI",
            sha="abc123",
            timeout_seconds=0,
            poll_interval_seconds=1,
            runner=runner,
        )
        == 1
    )


def test_missing_run_message_uses_requested_workflow() -> None:
    """Missing-run wait messages name the workflow requested by the caller."""
    decision = verify_ci_gate.evaluate_runs([], "abc123", workflow="Release")

    assert decision.state == "wait"
    assert "No Release run found" in decision.message


def test_main_rejects_non_positive_poll_interval(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI rejects non-positive poll intervals before polling GitHub."""
    with pytest.raises(SystemExit) as exc_info:
        verify_ci_gate.main(
            [
                "--repo",
                "GobbyAI/gobby",
                "--sha",
                "abc123",
                "--poll-interval-seconds",
                "0",
            ]
        )

    assert exc_info.value.code == 2
    assert "--poll-interval-seconds must be > 0" in capsys.readouterr().err


def test_in_progress_latest_run_waits_and_repolls() -> None:
    responses = [
        _runs_json(
            _run(
                created_at="2026-05-18T10:00:00Z",
                database_id=1,
                status="in_progress",
                conclusion=None,
            )
        ),
        _runs_json(
            _run(
                created_at="2026-05-18T10:00:00Z",
                database_id=1,
                conclusion="success",
            )
        ),
    ]
    clock = _FakeClock()
    sleep_calls: list[float] = []

    def runner(_command: Sequence[str]) -> str:
        return responses.pop(0)

    def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock.advance(seconds)

    assert (
        verify_ci_gate.wait_for_ci_gate(
            repo="GobbyAI/gobby",
            workflow="CI",
            sha="abc123",
            timeout_seconds=10,
            poll_interval_seconds=2,
            runner=runner,
            sleep=sleep,
            clock=clock.monotonic,
        )
        == 0
    )
    assert sleep_calls == [2]
    assert responses == []


def test_completed_non_success_conclusion_fails() -> None:
    runs = verify_ci_gate.parse_workflow_runs(
        _runs_json(
            _run(
                created_at="2026-05-18T10:00:00Z",
                database_id=1,
                conclusion="timed_out",
            )
        )
    )

    assert verify_ci_gate.evaluate_runs(runs, "abc123").state == "fail"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _run(
    *,
    created_at: str,
    database_id: int,
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "push",
    head_sha: str = "abc123",
    url: str = "https://github.com/GobbyAI/gobby/actions/runs/1",
) -> dict[str, object]:
    return {
        "createdAt": created_at,
        "databaseId": database_id,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "headSha": head_sha,
        "url": url,
    }


def _runs_json(*runs: dict[str, object]) -> str:
    return json.dumps(list(runs))
