"""Task claims derived from provider transcripts (#23385)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.tasks.transcript_evidence_models import TranscriptTaskClaim
from tests.tasks.test_transcript_evidence import (
    BASE_TIME,
    LOCAL_MACHINE_ID,
    _session,
    _write_jsonl,
)

CLAIMED = "7df89483-8c49-4277-b491-ffb1a1bf4c55"


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.sessions.machine_scope.get_machine_id", lambda: LOCAL_MACHINE_ID)


def _claude_call_tool(
    tool_name: str, arguments: dict[str, Any], result: Any
) -> list[dict[str, Any]]:
    """One proxied gobby-tasks call and its result, as Claude Code records them."""
    return [
        {
            "type": "assistant",
            "timestamp": BASE_TIME.isoformat(),
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_claim",
                        "name": "mcp__gobby__call_tool",
                        "input": {
                            "server_name": "gobby-tasks",
                            "tool_name": tool_name,
                            "arguments": arguments,
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": (BASE_TIME + timedelta(seconds=1)).isoformat(),
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_claim",
                        "content": [{"type": "text", "text": json.dumps(result)}],
                        "is_error": False,
                    }
                ],
            },
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "result", "expected_ref"),
    [
        pytest.param(
            "claim_task",
            {"task_id": "#23385"},
            {"success": True, "result": {"success": True, "task_id": CLAIMED}},
            CLAIMED,
            id="claim-resolves-to-uuid",
        ),
        pytest.param(
            "create_task",
            {"title": "t", "claim": True},
            {"success": True, "result": {"id": CLAIMED, "ref": "#23385"}},
            CLAIMED,
            id="create-and-claim",
        ),
        pytest.param(
            "claim_task",
            {"task_id": "#23385"},
            {"success": True, "result": {"error": "Task #23385 is claimed by another session"}},
            None,
            id="refused-claim",
        ),
        pytest.param(
            "create_task",
            {"title": "t", "claim": True},
            {
                "success": True,
                "result": {
                    "id": CLAIMED,
                    "warning": "claim=true ignored: current session could not be marked active",
                },
            },
            None,
            id="create-with-ignored-claim",
        ),
        pytest.param(
            "create_task",
            {"title": "t"},
            {"success": True, "result": {"id": CLAIMED}},
            None,
            id="create-without-claim",
        ),
    ],
)
async def test_claude_task_claims_are_derived(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    expected_ref: str | None,
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(transcript, _claude_call_tool(tool_name, arguments, result))

    evidence = await derive_transcript_evidence(
        _session("claude", transcript),
        BASE_TIME,
        default_validation_detection_config(),
        set(),
        str(tmp_path),
    )

    expected = (
        (TranscriptTaskClaim(task_ref=expected_ref, claimed_at=BASE_TIME + timedelta(seconds=1)),)
        if expected_ref
        else ()
    )
    assert evidence.task_claims == expected
