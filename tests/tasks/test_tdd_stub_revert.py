"""Gate 12 credits a RED proven by reverting exactly the stub that caused it."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 10, 15, tzinfo=UTC)
HANDLERS = "src/feature/handlers.py"
WORKING = "def mismatch(envelope):\n    return None\n"
STUB_BODY = "    raise NotImplementedError\n"
WORKING_BODY = "    return None\n"
# The test reaches the stub only through an HTTP boundary that turns the
# NotImplementedError into a status code.
TEST_SOURCE = (
    "from fastapi.testclient import TestClient\n"
    "from feature import app\n\n"
    "def test_feature():\n"
    "    response = TestClient(app).post('/hook', json={})\n"
    "    assert response.status_code == 200\n"
)
TEST = AcceptanceTest(
    reference="tests/test_feature.py::test_feature",
    path="tests/test_feature.py",
    symbol="test_feature",
    body=TEST_SOURCE,
)
COMMAND = f"pytest {TEST.reference} -q"
RED_OUTPUT = (
    "_______________________ test_feature _______________________\n"
    "E       assert 500 == 200\n"
    "tests/test_feature.py:6: AssertionError\n"
    "FAILED tests/test_feature.py::test_feature - assert 500 == 200\n"
)


def _write(path: str, order: int, source: str) -> TranscriptEdit:
    at = START + timedelta(seconds=order * 10)
    return TranscriptEdit(
        session_id="owner",
        source="claude",
        path=path,
        timestamp=at,
        order=order,
        tool_name="Write",
        source_after=source,
        source_confirmed=True,
        source_confirmed_at=at + timedelta(seconds=1),
    )


def _body_edit(order: int, old: str, new: str) -> TranscriptEdit:
    return replace(
        _write(HANDLERS, order, WORKING.replace(WORKING_BODY, new)),
        tool_name="Edit",
        source_fragment=new,
        source_replaced=old,
    )


def _run(order: int, *, red: bool, command: str = COMMAND) -> TranscriptValidationRun:
    at = START + timedelta(seconds=order * 10)
    return TranscriptValidationRun(
        session_id="owner",
        source="claude",
        command=command,
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure" if red else "success",
        started_at=at,
        completed_at=at + timedelta(seconds=2),
        order=order,
        exit_code=1 if red else 0,
        output=RED_OUTPUT if red else "1 passed",
    )


def _stub_revert_cycle(
    *,
    revert_body: str = WORKING_BODY,
    green_command: str = COMMAND,
    extra_edit: TranscriptEdit | None = None,
) -> TranscriptEvidence:
    """Production, test, stub at 2, RED at 4, revert at 6, GREEN at 8; odd orders interleave."""
    return TranscriptEvidence(
        edits=(
            replace(_write(HANDLERS, 0, WORKING), source_created=True),
            _write(TEST.path, 1, TEST_SOURCE),
            _body_edit(2, WORKING_BODY, STUB_BODY),
            _body_edit(6, STUB_BODY, revert_body),
            *((extra_edit,) if extra_edit is not None else ()),
        ),
        validation_runs=(_run(4, red=True), _run(8, red=False, command=green_command)),
    )


def test_stub_revert_credits_a_red_the_test_reaches_only_through_http() -> None:
    result = evaluate_tdd_evidence((TEST,), _stub_revert_cycle())

    assert result.passed, result.findings
    assert result.red_runs == (COMMAND,)
    assert result.green_runs == (COMMAND,)


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(
            _stub_revert_cycle(extra_edit=_write(TEST.path, 3, TEST_SOURCE)),
            id="test-edit-between-stub-and-red",
        ),
        pytest.param(
            _stub_revert_cycle(extra_edit=_write(TEST.path, 7, TEST_SOURCE)),
            id="test-edit-between-revert-and-green",
        ),
        pytest.param(
            _stub_revert_cycle(revert_body="    return {}\n"),
            id="revert-changes-beyond-the-stub",
        ),
        pytest.param(
            _stub_revert_cycle(green_command="pytest tests/test_feature.py -q"),
            id="green-names-different-tests",
        ),
        pytest.param(
            _stub_revert_cycle(extra_edit=_write("src/feature/other.py", 7, WORKING)),
            id="production-edit-between-revert-and-green",
        ),
    ],
)
def test_stub_revert_refuses_an_unproven_cycle(evidence: TranscriptEvidence) -> None:
    result = evaluate_tdd_evidence((TEST,), evidence)

    assert not result.passed
    assert result.red_runs == ()
