"""Author validation can cover a handed-off task without reopening edit ownership."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import derive_close_transcript_evidence
from gobby.tasks.close_checklist import evaluate_validation_commands
from tests.mcp_proxy.tools.tasks.test_close_evidence_sessions import (
    IMPLEMENTER,
    _claude_edit_session,
    _claude_shell_records,
    _context,
    _link,
)

_SUPPORT = "gobby.mcp_proxy.tools.tasks._close_evaluation_support"
_TEST = "tests/test_owned.py"
_SOURCE = "src/gobby/component.py"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "author",
        "author-copy",
        "author-source-only",
        "author-later-failure",
        "foreign",
        "different-bytes",
        "different-source-bytes",
        "stale",
        "unrelated",
        "broad",
        "failed",
        "wrapped",
        "unknown",
        "prelink",
        "no-candidate",
    ],
)
async def test_handed_off_author_run_requires_targeted_fresh_candidate_proof(
    tmp_path: Path, case: str
) -> None:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    owned = root / _TEST
    owned.write_text("def test_owned():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (root / "tests/test_other.py").write_text(
        "def test_other():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    (root / "settings.toml").write_text("enabled = true\n", encoding="utf-8")
    (root / _SOURCE).parent.mkdir(parents=True)
    (root / _SOURCE).write_text("VALUE = 2\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", "tests", "settings.toml", "src"),
        ("-c", "user.email=t@t", "-c", "user.name=Test", "commit", "-qm", "candidate"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    start = datetime(2026, 10, 15, 12, tzinfo=UTC)
    transcript = tmp_path / "author.jsonl"
    author = _claude_edit_session(transcript, IMPLEMENTER, owned, start + timedelta(seconds=5))
    records = [json.loads(transcript.read_text(encoding="utf-8"))]
    records += _claude_shell_records(
        start,
        10,
        "audit",
        f"cd {root} && uv run gobby test-types audit {_TEST} "
        "--baseline .gobby/test-types-baseline.json --fail-on-new",
        "Files scanned: 1\nErrors: 0\nNew errors: 0",
        0,
    )
    target = "tests/test_other.py" if case == "unrelated" else "tests" if case == "broad" else _TEST
    command = f"cd {root} && uv run pytest {target} -q"
    if case == "wrapped":
        command += " || true"
    records += _claude_shell_records(
        start,
        -10 if case == "prelink" else 30,
        "away-green",
        command,
        "1 failed" if case == "failed" else "1 passed in 0.1s",
        1 if case == "failed" else 0,
    )
    if case == "unknown":
        records[-1]["message"]["content"][0]["content"] = (
            "Command did not complete within its 600s timeout and was moved to the "
            f"background (ID: pending). Output is being written to: {tmp_path}/pending.output. "
            "You will be notified when it completes."
        )
    if case == "author-later-failure":
        records += _claude_shell_records(
            start,
            40,
            "unrelated-red",
            f"cd {root} && uv run pytest tests/test_other.py -q",
            "1 failed in 0.1s",
            1,
        )
    if case == "stale":
        edit = json.loads(json.dumps(records[0]))
        edit["timestamp"] = (start + timedelta(seconds=40)).isoformat()
        edit["message"]["content"][0]["id"] = "later-edit"
        records.append(edit)
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    if case == "different-bytes":
        owned.write_text("def test_owned():\n    assert 1 + 1 == 3\n", encoding="utf-8")
    if case == "different-source-bytes":
        (root / "settings.toml").write_text("enabled = false\n", encoding="utf-8")
    close_root = root
    if case in {"author-copy", "author-source-only"}:
        close_root = tmp_path / "close"
        subprocess.run(
            ["git", "clone", "--quiet", str(root), str(close_root)], check=True, capture_output=True
        )

    sessions = {IMPLEMENTER: author}
    if case == "foreign":
        sessions["other-owner"] = _claude_edit_session(
            tmp_path / "owner.jsonl", "other-owner", root / "unrelated.txt", start
        )
    ctx = _context([_link(IMPLEMENTER, "claimed", start.isoformat())], sessions)
    ctx.session_var_manager.get_variables.return_value = {
        "task_edited_file_checkouts": {"task": {str(root): [_TEST]}},
        "task_edited_checkout_times": {
            "task": {
                str(root): {
                    _TEST: (start + timedelta(seconds=40 if case == "stale" else 5)).timestamp()
                }
            }
        },
        "task_selection_history": [
            {"epoch": start.isoformat(), "task_id": "task"},
            {"epoch": (start + timedelta(seconds=20)).isoformat(), "task_id": "later-task"},
        ],
    }
    ctx.session_task_manager.get_session_tasks.return_value = []
    with (
        patch(
            f"{_SUPPORT}.resolve_validation_detection_config",
            return_value=default_validation_detection_config(),
        ),
        patch(f"{_SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{_SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id="other-owner" if case == "foreign" else IMPLEMENTER,
            closing_session_id=IMPLEMENTER,
            owner_window_start=start.isoformat(),
            task_edited_files={_TEST},
            repo_path=str(root),
        )
    # Gate 12's shared evidence still excludes the run made under the later claim.
    assert all(run.command != command for run in evidence.validation_runs)
    if case == "stale":
        assert any(edit.timestamp == start + timedelta(seconds=40) for edit in evidence.edits)
    if case == "unknown":
        assert any(run.outcome == "unknown" for run in evidence.off_task_author_runs)
    source_only = case == "author-source-only"
    with patch(
        "gobby.tasks.close_checklist.related_python_source_tests_in_pool",
        return_value={_SOURCE: (_TEST,)} if source_only else {},
    ):
        gate = evaluate_validation_commands(
            task_category="code",
            evidence=evidence,
            has_attributed_edits=True,
            changed_paths=(_SOURCE,) if source_only else (_TEST, "settings.toml"),
            close_root=str(close_root),
            candidate_commit_sha=None if case == "no-candidate" else candidate,
        )
    expected = case in {"author", "author-copy", "author-source-only", "author-later-failure"}
    assert gate.passed is expected, gate.message
    if source_only:
        assert gate.details["python_source_uncovered_tests"] == {}
    else:
        assert gate.details["pytest_uncovered_paths"] == ([] if expected else [_TEST])
