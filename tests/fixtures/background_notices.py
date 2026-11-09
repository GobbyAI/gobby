"""Owner transcripts that carry a Claude background command's tool use and terminal notice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def background_receipt_text(job_id: str, output_path: str) -> str:
    """The text Claude returns when it moves a command to the background."""
    return (
        f"Command running in background with ID: {job_id}. "
        f"Output is being written to: {output_path}. You will be notified when it completes."
    )


def _notice_row(
    *, tool_use_id: str, job_id: str, output_path: str, exit_code: int, uuid: str
) -> dict[str, Any]:
    status = "completed" if exit_code == 0 else "failed"
    verb = "completed" if exit_code == 0 else f"failed with exit code {exit_code}"
    summary = (
        f'Background command "Run" {verb}'
        if exit_code != 0
        else 'Background command "Run" completed (exit code 0)'
    )
    content = (
        "<task-notification>\n"
        f"<task-id>{job_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<output-file>{output_path}</output-file>\n"
        f"<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n"
        "</task-notification>"
    )
    return {
        "type": "user",
        "uuid": uuid,
        "message": {"role": "user", "content": content},
        "origin": {"kind": "task-notification", "producer": "session-task"},
    }


def _tool_use_row(tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": f"assistant-{tool_use_id}",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {}}],
        },
    }


def owner_transcript(
    tmp_path: Path,
    *,
    tool_use_id: str,
    job_id: str,
    output_path: str,
    exit_code: int | None = 0,
    name: str = "owner.jsonl",
) -> str:
    """Write an owner transcript with the call's tool use; add its notice when ``exit_code`` is set.

    ``exit_code=None`` leaves the notice out, as a call whose process has not ended.
    """
    rows: list[dict[str, Any]] = [_tool_use_row(tool_use_id)]
    if exit_code is not None:
        rows.append(
            _notice_row(
                tool_use_id=tool_use_id,
                job_id=job_id,
                output_path=output_path,
                exit_code=exit_code,
                uuid=f"notice-{tool_use_id}",
            )
        )
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return str(path)
