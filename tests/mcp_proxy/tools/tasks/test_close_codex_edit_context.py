"""Close evidence keeps the provider's checkout for relative patch calls."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import derive_close_transcript_evidence
from gobby.storage.session_models import Session
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.utils.machine_id import get_machine_id

_SUPPORT = "gobby.mcp_proxy.tools.tasks._close_evaluation_support"


def _record(offset: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": (
            datetime(2026, 10, 12, 2, tzinfo=UTC) + timedelta(seconds=offset)
        ).isoformat(),
        "type": kind,
        "payload": payload,
    }


def _patch_records(offset: int, path: str) -> list[dict[str, Any]]:
    patch_text = f"*** Begin Patch\n*** Add File: {path}\n+assert True\n*** End Patch"
    call_id = f"patch-{offset}"
    return [
        _record(
            offset,
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": call_id,
                "input": f"text(await tools.apply_patch({json.dumps(patch_text)}));",
            },
        ),
        _record(
            offset + 1,
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": call_id,
                "output": f"Success. Updated the following files:\nA {path}",
            },
        ),
    ]


def _run_records(offset: int, outcome: int) -> list[dict[str, Any]]:
    call_id = f"run-{offset}"
    return [
        _record(
            offset,
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": call_id,
                "arguments": json.dumps({"cmd": "uv run pytest tests/test_named.py -q"}),
            },
        ),
        _record(
            offset + 1,
            "response_item",
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    {
                        "exit_code": outcome,
                        "output": "_____ test_named _____\nE AssertionError\n"
                        "tests/test_named.py:2: AssertionError\nFAILED tests/test_named.py::test_named"
                        if outcome
                        else "1 passed in 0.1s",
                    }
                ),
            },
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("context", ["owned", "old", "session-meta", "foreign", "missing"])
async def test_relative_patch_red_green_survives_compact_and_snapshot(
    tmp_path: Path,
    context: str,
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    test_path, source_path = "tests/test_named.py", "src/named.py"
    transcript = tmp_path / "owner.jsonl"
    records = []
    if context != "missing":
        cwd = tmp_path / "foreign" if context == "foreign" else root
        records.append(
            _record(
                -10800 if context == "old" else 0,
                "session_meta" if context == "session-meta" else "turn_context",
                {"cwd": str(cwd), "model": "gpt-6"},
            )
        )
    records.extend(_patch_records(10, test_path))
    records.extend(_run_records(20, 1))
    records.extend(_patch_records(30, source_path))
    records.extend(_run_records(40, 0))
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    start = datetime(2026, 10, 12, 2, tzinfo=UTC)
    machine_id = get_machine_id()
    assert machine_id is not None
    session = Session(
        id="owner",
        external_id="owner",
        machine_id=machine_id,
        source="codex",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=start,
        updated_at=start,
    )
    ctx = MagicMock()
    ctx.config = None
    ctx.session_manager.get.return_value = session
    ctx.session_task_manager.get_task_sessions.return_value = [
        {"session_id": "owner", "action": "claimed", "created_at": start.isoformat()}
    ]
    ctx.session_task_manager.get_session_tasks.return_value = [
        {"task_id": "task", "action": "claimed", "created_at": start.isoformat()}
    ]
    ctx.session_var_manager.get_variables.return_value = {
        "task_edited_file_checkouts": {"task": {str(root): [test_path, source_path]}}
    }
    with (
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
        patch(
            f"{_SUPPORT}.resolve_validation_detection_config",
            return_value=default_validation_detection_config(),
        ),
        patch(
            f"{_SUPPORT}.committable_task_paths_async",
            new=AsyncMock(return_value={test_path, source_path}),
        ),
        patch("gobby.tasks.transcript_evidence_cache.agent_home_cache_root", return_value=tmp_path),
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id="owner",
            closing_session_id="owner",
            owner_window_start=start.isoformat(),
            task_edited_files={test_path, source_path},
            repo_path=str(root),
        )
        # The next parse is a suffix hydrated from the durable snapshot, just as
        # after the owner's compaction. No new cwd appears in that suffix.
        with transcript.open("a") as stream:
            stream.write(json.dumps(_record(50, "event_msg", {"type": "context_compacted"})) + "\n")
            for record in _patch_records(60, test_path):
                stream.write(json.dumps(record) + "\n")
            for record in _run_records(70, 0):
                stream.write(json.dumps(record) + "\n")
        resumed = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id="owner",
            closing_session_id="owner",
            owner_window_start=start.isoformat(),
            task_edited_files={test_path, source_path},
            repo_path=str(root),
        )
    if context in {"foreign", "missing"}:
        assert not evidence.edits
        assert not resumed.edits
        return
    assert [edit.path for edit in resumed.edits] == [test_path, source_path, test_path]
    result = evaluate_tdd_evidence(
        (
            AcceptanceTest(
                reference=f"{test_path}::test_named",
                path=test_path,
                symbol="test_named",
                body="def test_named():\n    assert named()\n",
            ),
        ),
        resumed,
    )
    assert result.passed, result.findings
    assert result.red_runs
    assert result.green_runs
