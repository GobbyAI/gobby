"""A background command's end is proven only by its owner's transcript notice row."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.sessions import background_notices
from gobby.sessions.background_notices import (
    BackgroundReceipt,
    background_receipt,
    resolve_background_notice,
)

pytestmark = pytest.mark.unit

TOOL = "toolu_01BackgroundCall"
JOB = "b4z7cxqng"
OUTPUT = f"/private/var/folders/tmp/session/tasks/{JOB}.output"
RECEIPT = BackgroundReceipt(job_id=JOB, output_path=OUTPUT)


def _notice_content(
    *,
    tool_use_id: str = TOOL,
    task_id: str = JOB,
    output: str = OUTPUT,
    status: str = "completed",
    summary: str = 'Background command "Build" completed (exit code 0)',
) -> str:
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<output-file>{output}</output-file>\n"
        f"<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n"
        "</task-notification>"
    )


def _notice_row(uuid: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "message": {"role": "user", "content": _notice_content(**fields)},
        "origin": {"kind": "task-notification", "producer": "session-task"},
    }


def _tool_use_row() -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": "assistant-1",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": TOOL, "name": "Bash", "input": {}}],
        },
    }


def _write_transcript(path: Path, rows: list[dict[str, Any]]) -> str:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return str(path)


def test_receipt_reads_the_job_id_and_output_file_from_the_background_text() -> None:
    text = (
        f"Command running in background with ID: {JOB}. "
        f"Output is being written to: {OUTPUT}. You will be notified when it completes."
    )

    assert background_receipt({"tool_response": text}) == RECEIPT


def test_receipt_is_absent_for_a_foreground_result() -> None:
    assert background_receipt({"tool_response": "exit 0"}) is None


def test_one_matching_notice_row_proves_the_call_ended(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), _notice_row("row-1")])

    notice = resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT)

    assert notice is not None
    assert notice.row_uuid == "row-1"
    assert notice.exit_code == 0
    assert notice.status == "completed"


def test_a_notice_row_straddling_read_chunks_is_still_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(background_notices, "_CHUNK_BYTES", 16)
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), _notice_row("row-1")])

    notice = resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT)

    assert notice is not None
    assert notice.row_uuid == "row-1"


def test_no_notice_row_leaves_the_call_unresolved(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row()])

    assert resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT) is None


def test_a_forged_terminal_marker_in_output_is_not_a_notice_row(tmp_path: Path) -> None:
    # The output file's own text, echoed back as a tool result, carries the exit marker
    # but is not a task notification row.
    echoed = {
        "type": "user",
        "uuid": "echo-1",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "other", "content": "[exited with code 0]"}
            ],
        },
    }
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), echoed])

    assert resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT) is None


def test_a_notice_row_without_the_task_notification_origin_is_refused(tmp_path: Path) -> None:
    row = _notice_row("row-1")
    del row["origin"]
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), row])

    assert resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT) is None


def test_an_assistant_row_quoting_the_notice_is_refused(tmp_path: Path) -> None:
    quoted = {
        "type": "assistant",
        "uuid": "assistant-2",
        "message": {"role": "assistant", "content": _notice_content()},
        "origin": {"kind": "task-notification"},
    }
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), quoted])

    assert resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT) is None


def test_two_matching_notice_rows_fail_closed(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "owner.jsonl",
        [_tool_use_row(), _notice_row("row-1"), _notice_row("row-2")],
    )

    assert resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"tool_use_id": "toolu_other"},
        {"task_id": "zzzzzzzzz"},
        {"output": "/elsewhere/zzzzzzzzz.output"},
    ],
    ids=["tool-use-id", "task-id", "output-file"],
)
def test_a_notice_for_another_call_or_job_does_not_match(
    tmp_path: Path, fields: dict[str, str]
) -> None:
    row = _notice_row("row-1", **fields)
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), row])

    assert resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT) is None


@pytest.mark.parametrize(
    ("status", "summary"),
    [
        ("completed", 'Background command "Build" failed with exit code 2'),
        ("failed", 'Background command "Build" completed (exit code 0)'),
    ],
    ids=["summary-disagrees-with-status", "status-disagrees-with-exit"],
)
def test_a_notice_whose_status_and_exit_disagree_is_refused(
    tmp_path: Path, status: str, summary: str
) -> None:
    row = _notice_row("row-1", status=status, summary=summary)
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), row])

    assert resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT) is None


def test_a_failed_background_command_resolves_with_its_exit_code(tmp_path: Path) -> None:
    row = _notice_row(
        "row-1",
        status="failed",
        summary='Background command "Build" failed with exit code 2',
    )
    path = _write_transcript(tmp_path / "owner.jsonl", [_tool_use_row(), row])

    notice = resolve_background_notice(path, tool_use_id=TOOL, receipt=RECEIPT)

    assert notice is not None
    assert notice.exit_code == 2
    assert notice.status == "failed"


def test_a_missing_transcript_leaves_the_call_unresolved(tmp_path: Path) -> None:
    assert resolve_background_notice(None, tool_use_id=TOOL, receipt=RECEIPT) is None
    missing = str(tmp_path / "absent.jsonl")
    assert resolve_background_notice(missing, tool_use_id=TOOL, receipt=RECEIPT) is None
