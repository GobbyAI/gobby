"""Persisted selection isolates validation, including automatic returns and delayed runs."""

from dataclasses import replace
from datetime import timedelta

import pytest

from gobby.mcp_proxy.tools.tasks._close_evaluation_support import _task_presence, _while_on_task
from gobby.tasks.transcript_evidence_models import TranscriptEvidence
from gobby.workflows.task_claim_state import record_task_selection
from tests.tasks.test_close_checklist import BASE_TIME, _run

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("task_id,expected_orders", [("A", (2, 8)), ("B", (5,))])
def test_selection_history_isolates_runs_and_restores_surviving_claim(
    task_id: str, expected_orders: tuple[int, ...]
) -> None:
    history = [
        {"task_id": "A", "epoch": BASE_TIME.isoformat()},
        {"task_id": "B", "epoch": (BASE_TIME + timedelta(seconds=3)).isoformat()},
        # Closing B automatically selects the sole remaining claim A.
        {"task_id": "A", "epoch": (BASE_TIME + timedelta(seconds=6)).isoformat()},
    ]
    delayed_a = replace(_run(2), completed_at=BASE_TIME + timedelta(seconds=9))
    b_failure = _run(5, outcome="failure")
    returned_a = _run(8)
    evidence = TranscriptEvidence(
        validation_runs=(delayed_a, b_failure, returned_a),
        command_runs=(delayed_a, b_failure, returned_a),
    )

    presence = _task_presence([], task_id, BASE_TIME, [], selection_history=history)
    assert presence is not None
    scoped = _while_on_task(evidence, presence)

    assert tuple(run.order for run in scoped.validation_runs) == expected_orders
    assert scoped.command_runs == scoped.validation_runs


def test_selection_at_window_start_retains_its_predecessor() -> None:
    history = [
        {"task_id": "A", "epoch": BASE_TIME.isoformat()},
        {"task_id": "B", "epoch": (BASE_TIME + timedelta(seconds=3)).isoformat()},
        {"task_id": "A", "epoch": (BASE_TIME + timedelta(seconds=6)).isoformat()},
    ]
    window_start = BASE_TIME + timedelta(seconds=4)

    presence = _task_presence([], "A", window_start, [], selection_history=history)
    assert presence == [
        (window_start.timestamp(), False),
        ((BASE_TIME + timedelta(seconds=6)).timestamp(), True),
    ]


@pytest.mark.parametrize(
    "selected_after_restart,expected_orders",
    [("A", (2, 4, 8)), ("B", (2, 4))],
)
def test_history_begun_after_window_start_keeps_earlier_runs(
    selected_after_restart: str, expected_orders: tuple[int, ...]
) -> None:
    # A session working on A before selection history existed gets its first
    # entry at its first variable write after the restart (#23781).
    variables: dict[str, object] = {"active_task_id": selected_after_restart}
    restarted_at = BASE_TIME + timedelta(seconds=6)
    record_task_selection(variables, "A", restarted_at.isoformat())
    red = _run(2, outcome="failure")
    green = _run(4)
    after_restart = _run(8)
    evidence = TranscriptEvidence(
        validation_runs=(red, green, after_restart),
        command_runs=(red, green, after_restart),
    )

    presence = _task_presence(
        [], "A", BASE_TIME, [], selection_history=variables["task_selection_history"]
    )
    scoped = evidence if presence is None else _while_on_task(evidence, presence)

    assert tuple(run.order for run in scoped.validation_runs) == expected_orders
    assert scoped.command_runs == scoped.validation_runs
