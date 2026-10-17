"""Author validation can cover a handed-off task without reopening edit ownership."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import derive_close_transcript_evidence
from gobby.tasks.close_checklist import evaluate_validation_commands
from tests.mcp_proxy.tools.tasks.test_close_codex_edit_context import _patch_records, _record
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
        "shared-stale",
        "shared-source-stale",
        "shared-before-run",
        "reclaimed-shared-stale",
        "reclaimed-shared-source-stale",
        "reclaimed-shared-before-run",
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
    reclaimed = case.startswith("reclaimed-")
    shared = case.startswith(("shared-", "reclaimed-shared-"))
    shared_path = "settings.toml" if case.endswith("shared-source-stale") else _TEST
    shared_edit_seconds = 25 if case.endswith("shared-before-run") else 40
    if case == "reclaimed-shared-before-run":
        records += _claude_shell_records(
            start,
            65,
            "reclaim-audit",
            f"cd {root} && uv run gobby test-types audit {_TEST} "
            "--baseline .gobby/test-types-baseline.json --fail-on-new",
            "Files scanned: 1\nErrors: 0\nNew errors: 0",
            0,
        )
    if shared:
        edit = json.loads(json.dumps(records[0]))
        edit["timestamp"] = (start + timedelta(seconds=shared_edit_seconds)).isoformat()
        edit["message"]["content"][0]["id"] = "shared-away-edit"
        edit["message"]["content"][0]["input"]["file_path"] = str(root / shared_path)
        records.append(edit)
        records.sort(key=lambda record: record["timestamp"])
        (root / shared_path).write_text("# post-edit candidate bytes\n", encoding="utf-8")
        subprocess.run(["git", "add", shared_path], cwd=root, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=Test", "commit", "-qm", "away edit"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        candidate = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
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
    claim_start = start + timedelta(seconds=60) if reclaimed else start
    ctx = _context([_link(IMPLEMENTER, "claimed", claim_start.isoformat())], sessions)
    ctx.session_var_manager.get_variables.return_value = {
        "task_edited_file_checkouts": {
            "task": {str(root): list(dict.fromkeys((_TEST, shared_path)))},
            **({"later-task": {str(root): [shared_path]}} if shared else {}),
        },
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
            *([{"epoch": claim_start.isoformat(), "task_id": "task"}] if reclaimed else []),
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
            owner_window_start=claim_start.isoformat(),
            task_edited_files={_TEST},
            repo_path=str(root),
        )
    # Gate 12's shared evidence still excludes the run made under the later claim.
    assert all(run.command != command for run in evidence.validation_runs)
    if reclaimed:
        # Reclaim must retain the earlier away run and its freshness veto pool.
        assert any(run.command == command for run in evidence.off_task_author_runs)
        assert any(
            edit.timestamp == start + timedelta(seconds=shared_edit_seconds)
            for edit in evidence.off_task_author_edits
        )
    if case == "stale":
        assert any(edit.timestamp == start + timedelta(seconds=40) for edit in evidence.edits)
    if shared:
        # The later claim's shared-path edit stays as unattributed safety evidence.
        assert [
            edit.task_attributed
            for edit in evidence.edits
            if edit.timestamp == start + timedelta(seconds=shared_edit_seconds)
        ] == [False]
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
    expected = case in {
        "author",
        "author-copy",
        "author-source-only",
        "author-later-failure",
        "reclaimed-shared-before-run",
    }
    assert gate.passed is expected, gate.message
    if source_only:
        assert gate.details["python_source_uncovered_tests"] == {}
    else:
        credited = expected or case == "shared-before-run"
        assert gate.details["pytest_uncovered_paths"] == ([] if credited else [_TEST])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["author", "foreign", "different-bytes"])
async def test_original_codex_five_file_packet_credits_only_its_author_candidate(
    tmp_path: Path, case: str
) -> None:
    """Reproduce #24506's existing 861-pass packet without executing that packet."""
    paths = (
        "tests/tasks/test_acceptance_rust_modules.py",
        "tests/tasks/test_acceptance_artifacts.py",
        "tests/mcp_proxy/tools/tasks/test_close_candidate.py",
        "tests/mcp_proxy/tools/tasks/test_close_codex_edit_context.py",
        "tests/mcp_proxy/tools/tasks/test_close_evidence_sessions.py",
    )
    root = tmp_path / "repo"
    for path in (*paths, _SOURCE):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 2\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.email=t@t", "-c", "user.name=Test", "commit", "-qm", "candidate"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    start = datetime(2026, 10, 12, 2, tzinfo=UTC)
    transcript = tmp_path / "author.jsonl"
    author = replace(
        _claude_edit_session(transcript, IMPLEMENTER, root / paths[0], start), source="codex"
    )
    command = (
        "RTK_DISABLED=1 "
        "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
        "GOBBY_TEST_PROTECT=1 uv run pytest "
        + " ".join(paths)
        + " -q --no-cov -p no:cacheprovider --timeout=60 --timeout-method=signal "
        "--session-timeout=120"
    )
    records = [_record(0, "session_meta", {"cwd": str(root)})]
    for offset, path in enumerate((*paths, _SOURCE), 5):
        records.extend(_patch_records(offset, path))
    for offset, call_id, argv, output in (
        (
            15,
            "audit",
            "uv run gobby test-types audit tests/ "
            "--baseline .gobby/test-types-baseline.json --fail-on-new",
            "Files scanned: 5\nErrors: 0\nNew errors: 0",
        ),
        (30, "original-861", command, "861 passed in 51.24s"),
    ):
        records.extend(
            [
                _record(
                    offset,
                    "response_item",
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": call_id,
                        "arguments": json.dumps({"cmd": argv}),
                    },
                ),
                _record(
                    offset + 1,
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"exit_code": 0, "output": output}),
                    },
                ),
            ]
        )
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    if case == "different-bytes":
        (root / _SOURCE).write_text("VALUE = 3\n", encoding="utf-8")
    sessions = {IMPLEMENTER: author}
    owner = IMPLEMENTER
    if case == "foreign":
        owner = "other-owner"
        sessions[owner] = _claude_edit_session(
            tmp_path / "other.jsonl", owner, root / "other.txt", start
        )
    ctx = _context([_link(IMPLEMENTER, "claimed", start.isoformat())], sessions)
    ctx.session_var_manager.get_variables.return_value = {
        "task_edited_file_checkouts": {"task": {str(root): [*paths, _SOURCE]}},
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
            owner_session_id=owner,
            closing_session_id=IMPLEMENTER,
            owner_window_start=start.isoformat(),
            task_edited_files={*paths, _SOURCE},
            repo_path=str(root),
        )
    assert all(run.command != command for run in evidence.validation_runs)
    assert any(run.command == command for run in evidence.excluded_runs)
    assert any(run.command == command for run in evidence.off_task_author_runs) is (
        case != "foreign"
    )
    with patch(
        "gobby.tasks.close_checklist.related_python_source_tests_in_pool",
        return_value={_SOURCE: paths},
    ):
        gate = evaluate_validation_commands(
            task_category="code",
            evidence=evidence,
            has_attributed_edits=True,
            changed_paths=(*paths, _SOURCE),
            close_root=str(root),
            candidate_commit_sha=candidate,
        )
    assert gate.passed is (case == "author"), gate.message
    assert gate.details["pytest_uncovered_paths"] == ([] if case == "author" else sorted(paths))
    assert {
        source: set(tests)
        for source, tests in gate.details["python_source_uncovered_tests"].items()
    } == ({} if case == "author" else {_SOURCE: set(paths)})
    assert any(record["command"] == command for record in gate.details["excluded_runs"]) is (
        case != "author"
    )
