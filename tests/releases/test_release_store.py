"""The seat's release lease maps daemon answers onto refusals, outages and generations.

A refused call is a lost lease; an unanswered one is a retry. The seat tracks the
generation the daemon hands back, and it files regressions through gobby-tasks as the
Release Manager session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.releases.batch import Checkout, ReleaseStoreUnavailable
from gobby.releases.store import DaemonReleaseLease
from gobby.releases.window import ReleaseWindowRefused

pytestmark = pytest.mark.unit

TOKEN = "0" * 32
SEAT = "release-manager-session"


def _lease(
    answers: list[tuple[int, object]], calls: list[tuple[str, str, str, dict[str, object]]]
) -> DaemonReleaseLease:
    async def call(
        session_id: str, server: str, tool: str, arguments: dict[str, object]
    ) -> tuple[int, object]:
        calls.append((session_id, server, tool, arguments))
        return answers.pop(0)

    return DaemonReleaseLease(call, owner_session_id=SEAT, token=TOKEN, generation=3)


def _answered(result: dict[str, object]) -> tuple[int, object]:
    """A successful release call as the daemon route answers it: the tool's payload under result."""
    return 200, {"success": True, "result": result}


async def test_a_refused_call_raises_the_daemon_code() -> None:
    refusal = {"success": False, "error": "stale", "error_code": "release_window_stale"}
    lease = _lease([(200, refusal)], [])

    with pytest.raises(ReleaseWindowRefused) as raised:
        await lease.checkout()

    assert raised.value.code == "release_window_stale"


@pytest.mark.parametrize(
    "answer",
    [(0, None), (500, {"detail": "boom"}), (200, {"success": False, "error": "no code"})],
)
async def test_an_unanswered_call_is_an_outage_not_a_lost_lease(
    answer: tuple[int, object],
) -> None:
    lease = _lease([answer], [])

    with pytest.raises(ReleaseStoreUnavailable):
        await lease.checkout()


async def test_every_call_carries_the_owner_session_and_the_current_lease() -> None:
    calls: list[tuple[str, str, str, dict[str, object]]] = []
    candidate = "c" * 40
    answer: dict[str, object] = {"root": "/repo", "candidate_sha": candidate}
    lease = _lease([_answered(answer)], calls)

    assert await lease.checkout() == Checkout(Path("/repo"), candidate)

    assert calls == [
        (SEAT, "gobby-agents", "release_batch_checkout", {"token": TOKEN, "generation": 3})
    ]


async def test_a_malformed_run_list_is_an_outage_not_an_empty_capture() -> None:
    lease = _lease([_answered({"run_ids": "run-1"})], [])

    with pytest.raises(ReleaseStoreUnavailable):
        await lease.active_runs()


async def test_renew_adopts_the_generation_the_daemon_returns() -> None:
    calls: list[tuple[str, str, str, dict[str, object]]] = []
    window: dict[str, Any] = {"window": {"generation": 4}}
    lease = _lease([_answered(window)], calls)

    await lease.renew()

    assert lease.generation == 4
    assert calls == [
        (SEAT, "gobby-agents", "renew_release_window", {"token": TOKEN, "generation": 3})
    ]


async def test_regression_filing_reuses_the_open_task_for_the_candidate() -> None:
    calls: list[tuple[str, str, str, dict[str, object]]] = []
    listing = {"tasks": [{"id": "task-9"}], "count": 1}
    lease = _lease([_answered(listing)], calls)

    task = await lease.file_regression("c" * 40, "b" * 40, "checks", "lint failed")

    assert task == "task-9"
    assert [(server, tool) for _s, server, tool, _a in calls] == [("gobby-tasks", "list_tasks")]
    assert calls[0][3]["label"] == f"release-regression-{'c' * 12}"


async def test_regression_filing_creates_one_bug_for_the_candidate() -> None:
    calls: list[tuple[str, str, str, dict[str, object]]] = []
    lease = _lease(
        [
            _answered({"tasks": [], "count": 0}),
            _answered({"id": "task-10", "seq_num": 10}),
        ],
        calls,
    )

    task = await lease.file_regression("c" * 40, "b" * 40, "smokes", "health failed")

    assert task == "task-10"
    assert [tool for _s, _server, tool, _a in calls] == ["list_tasks", "create_task"]
    created = calls[1][3]
    assert created["labels"] == [f"release-regression-{'c' * 12}"]
    assert (created["task_type"], created["priority"], created["category"]) == ("bug", 1, "code")
    assert created["implementation_domain"] == "backend"
    assert "smokes" in str(created["validation_criteria"])
    assert calls[1][0] == SEAT
