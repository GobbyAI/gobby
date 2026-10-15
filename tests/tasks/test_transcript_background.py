"""Claude background-receipt notifications parse only plain, declaration-free XML."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.tasks.transcript_background import _notification, recover_background_receipt
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun

pytestmark = pytest.mark.unit


def _pending_run() -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id="session-1",
        source="claude",
        command="uv run pytest tests/tasks/test_example.py -q",
        categories=("pytest",),
        matcher_id="pytest",
        label="pytest",
        outcome="unknown",
        started_at=datetime(2026, 10, 2, tzinfo=UTC),
        completed_at=datetime(2026, 10, 2, tzinfo=UTC),
        order=1,
        unknown_reason="background command pending",
        provider_call_id="toolu-1",
        background_job_id="job-1",
        background_output_path="/tmp/job-1.out",
    )


def _notice(summary: str, status: str = "completed") -> ParsedMessage:
    return _message(
        "<task-notification>"
        "<task-id>job-1</task-id><tool-use-id>toolu-1</tool-use-id>"
        f"<output-file>/tmp/job-1.out</output-file><status>{status}</status>"
        f"<summary>{summary}</summary></task-notification>"
    )


def test_completed_parenthesized_exit_code_notice_settles_receipt() -> None:
    original = _pending_run()
    runs = [original]
    message = _notice(f'Background command "{original.command}" completed (exit code 0)')

    recover_background_receipt(runs, message, pending=None, order=9)

    assert runs == [
        replace(
            original,
            outcome="success",
            exit_code=0,
            unknown_reason=None,
            completed_at=message.timestamp,
            order=9,
        )
    ]


@pytest.mark.parametrize(
    ("suffix", "status", "command_matches", "expected_exit"),
    [
        ("completed with exit code 0", "completed", True, 0),
        ("failed with exit code 1", "failed", True, 1),
        ("failed with exit code -9", "failed", True, -9),
        ("completed (exit code 0)", "completed", True, 0),
        ("completed (exit code 0)", "completed", False, None),
        ("completed with exit code 0", "completed", False, None),
        ("completed", "completed", True, None),
        ("completed (exit code)", "completed", True, None),
        ("completed (exit code 0)", "failed", True, None),
        ("failed with exit code 1", "completed", True, None),
    ],
)
def test_notice_forms_and_mismatches(
    suffix: str, status: str, command_matches: bool, expected_exit: int | None
) -> None:
    original = _pending_run()
    command = original.command if command_matches else original.command.replace(".py", "Xpy")
    runs = [original]
    message = _notice(f'Background command "{command}" {suffix}', status)

    recover_background_receipt(runs, message, pending=None, order=9)

    expected = original
    if expected_exit is not None:
        expected = replace(
            original,
            outcome="success" if expected_exit == 0 else "failure",
            exit_code=expected_exit,
            unknown_reason=None,
            completed_at=message.timestamp,
            order=9,
        )
    assert runs == [expected]


@pytest.mark.parametrize(
    "original",
    [
        replace(_pending_run(), background_job_id="other-job"),
        replace(_pending_run(), provider_call_id="other-call"),
        replace(_pending_run(), background_output_path="/tmp/other.out"),
        replace(_pending_run(), completed_at=datetime(2026, 10, 4, tzinfo=UTC)),
    ],
    ids=["job", "tool-call", "output-path", "stale-notice"],
)
def test_parenthesized_notice_preserves_receipt_binding(original: TranscriptValidationRun) -> None:
    runs = [original]
    message = _notice(f'Background command "{original.command}" completed (exit code 0)')

    recover_background_receipt(runs, message, pending=None, order=9)

    assert runs == [original]


_FIELDS = (
    "<task-id>job-1</task-id><tool-use-id>toolu-1</tool-use-id>"
    "<output-file>/tmp/job-1.out</output-file><status>failed</status>"
    '<summary>Background command "pytest" failed with exit code 1</summary>'
)


def _message(content: str) -> ParsedMessage:
    return ParsedMessage(
        index=0,
        role="user",
        content=content,
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime(2026, 10, 3, tzinfo=UTC),
        raw_json={},
    )


def test_plain_notification_yields_its_fields() -> None:
    notice = _notification(_message(f"<task-notification>{_FIELDS}</task-notification>"))
    assert notice == {
        "task-id": "job-1",
        "tool-use-id": "toolu-1",
        "output-file": "/tmp/job-1.out",
        "status": "failed",
        "summary": 'Background command "pytest" failed with exit code 1',
    }


@pytest.mark.parametrize(
    "content",
    [
        # A DTD after the root tag opens, carrying an internal entity expansion.
        f'<task-notification><!DOCTYPE x [<!ENTITY e "job-1">]>{_FIELDS}</task-notification>',
        # An external entity reference smuggled into a field.
        '<task-notification><!ENTITY xxe SYSTEM "file:///etc/passwd">'
        f"{_FIELDS}<summary>&xxe;</summary></task-notification>",
    ],
    ids=["internal-entity", "external-entity"],
)
def test_notification_with_a_declaration_is_rejected(content: str) -> None:
    assert _notification(_message(content)) is None
