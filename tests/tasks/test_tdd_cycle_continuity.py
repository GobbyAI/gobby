"""Task-level TDD cycles survive handoff and compacted runner output."""

import ast
import gzip
import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _pytest._io import TerminalWriter
from _pytest.assertion.rewrite import rewrite_asserts
from _pytest.config import get_config

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    claimed_session_window_start,
    derive_close_transcript_evidence,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.acceptance_artifacts import AcceptanceTest
from gobby.tasks.close_test_coverage import (
    CloseCandidate,
    candidate_pytest_paths,
    drop_foreign_runs,
)
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence import (
    _derive_transcript_evidence_sync,
    _derive_transcript_path_evidence,
)
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_tool_arguments import match_task_file
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import task_checkout_paths_in_window

START = datetime(2026, 10, 14, tzinfo=UTC)
TEST_PATH = "tests/test_feature.py"
SUPPORT = "gobby.mcp_proxy.tools.tasks._close_evaluation_support"


@pytest.mark.parametrize("reclaim", [False, True], ids=["same_claim", "released_and_reclaimed"])
def test_owner_window_survives_in_session_claim_restamp(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    reclaim: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    links = SessionTaskManager(temp_db)
    session = session_manager.register(
        external_id="owner-window",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Preserve the current claim window",
        validation_criteria="Repeated claims preserve the original evidence window.",
    )
    tasks.claim_task(task.id, session.id)
    # Set a deterministic original claim time in isolated fixture state.
    temp_db.execute(
        "UPDATE session_tasks SET created_at = %s WHERE session_id = %s AND task_id = %s "
        "AND action = 'claimed'",
        (START, session.id, task.id),
    )
    ctx = MagicMock(session_task_manager=links)
    original = claimed_session_window_start(ctx, tasks.get_task(task.id), task.id)
    assert original == START.isoformat()
    if reclaim:
        tasks.release_task_claim(task.id)
    tasks.claim_task(task.id, session.id)
    # Session-start/link restoration must remain idempotent within this claim.
    links.link_task(session.id, task.id, "claimed")
    current = claimed_session_window_start(ctx, tasks.get_task(task.id), task.id)
    assert current is not None
    if reclaim:
        assert datetime.fromisoformat(current) > START
    else:
        assert current == original


@pytest.mark.parametrize("reclaim", [False, True], ids=["same_claim", "released_and_reclaimed"])
async def test_owner_history_is_scoped_to_current_claim(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    reclaim: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    links = SessionTaskManager(temp_db)
    variables = SessionVariableManager(temp_db)
    session = session_manager.register(
        external_id="owner-history-window",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Scope owner history to its claim",
        validation_criteria="Earlier claim paths are excluded after release and reclaim.",
    )
    tasks.claim_task(task.id, session.id)
    temp_db.execute(
        "UPDATE session_tasks SET created_at = %s WHERE session_id = %s AND task_id = %s "
        "AND action = 'claimed'",
        (START, session.id, task.id),
    )
    root = str(tmp_path.resolve())
    old_path, new_path = "tests/old_claim.py", "tests/current_claim.py"
    for path in (old_path, new_path):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    variables.record_edited_files(
        session.id, [old_path], checkout_root=root, edited_at=START.timestamp() + 1
    )
    if reclaim:
        tasks.release_task_claim(task.id)
        tasks.claim_task(task.id, session.id)
    ctx = MagicMock()
    ctx.config = None
    ctx.session_manager = session_manager
    ctx.session_task_manager = links
    ctx.session_var_manager = variables
    window = claimed_session_window_start(ctx, tasks.get_task(task.id), task.id)
    assert window is not None
    variables.record_edited_files(
        session.id,
        [new_path],
        checkout_root=root,
        edited_at=datetime.fromisoformat(window).timestamp() + 2,
    )
    expected = {new_path} if reclaim else {old_path, new_path}
    observed: list[set[str]] = []

    async def parse(*args: Any, **kwargs: Any) -> TranscriptEvidence:
        # A legacy proof probe may inspect the old path, but only the final
        # owner projection (including the live new path) may credit pairs.
        if new_path in args[3]:
            observed.append({path for _, path in args[5]})
        return TranscriptEvidence()

    with (
        patch(f"{SUPPORT}.resolve_validation_detection_config"),
        patch(f"{SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{SUPPORT}._derive_session_evidence_at_sync_point", side_effect=parse),
        patch(f"{SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        await derive_close_transcript_evidence(
            ctx,
            task_id=task.id,
            owner_session_id=session.id,
            closing_session_id=session.id,
            owner_window_start=window,
            task_edited_files={new_path},
            repo_path=root,
        )
    assert observed == [expected]


@pytest.mark.parametrize("proof", ["exact_pair", "path_only", "missing", "old", "nonfinite"])
def test_owner_history_window_requires_checkout_specific_proof(proof: str) -> None:
    path = "tests/shared.py"
    pairs = frozenset({("/old-checkout", path), ("/current-checkout", path)})
    variables: dict[str, Any] = {"task_edited_file_times": {"task": {path: 200.0}}}
    if proof != "path_only":
        stamp = {"exact_pair": 200.0, "missing": None, "old": 50.0, "nonfinite": float("inf")}[
            proof
        ]
        variables["task_edited_checkout_times"] = {"task": {"/current-checkout": {path: stamp}}}
    assert task_checkout_paths_in_window(variables, "task", pairs, 100.0) == (
        frozenset({("/current-checkout", path)}) if proof == "exact_pair" else frozenset()
    )


@pytest.mark.parametrize(
    "copy", ["matching", "different-regression", "different-source", "different-test", "unrelated"]
)
@pytest.mark.parametrize("location", ["uv_directory", "effective_cwd"])
def test_claimed_session_main_checkout_run_credited_when_bytes_match(
    tmp_path: Path, copy: str, location: str
) -> None:
    from gobby.tasks.close_checklist import evaluate_validation_commands

    repo, exported = tmp_path / "repo", tmp_path / "exported"
    source = "src/feature.py"
    regression = "tests/test_regression.py"
    files = {
        TEST_PATH: "def test_feature(): assert feature() == 1\n",
        source: "def feature(): return 1\n",
        regression: "def test_regression(): assert feature() > 0\n",
    }
    for root in (repo, exported):
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    for args in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    candidate = CloseCandidate(str(repo), sha)
    # Candidate blobs remain authoritative over later edits in the closing checkout.
    (repo / source).write_text("def feature(): return 99\n")
    if copy == "different-source":
        (exported / source).write_text("def feature(): return 0\n")
    elif copy == "different-test":
        (exported / TEST_PATH).write_text("def test_feature(): assert feature() == 0\n")
    elif copy == "different-regression":
        (exported / regression).write_text("def test_regression(): assert feature() == 0\n")
    targets = regression if copy == "unrelated" else f"{TEST_PATH} {regression}"
    run = replace(
        _run(5, red=False),
        command=(
            f"uv run --directory {exported} --no-sync pytest {targets} -v -n0 --no-cov"
            if location == "uv_directory"
            else f"uv run --no-sync pytest {targets} -v -n0 --no-cov"
        ),
        workdir=str(repo if location == "uv_directory" else exported),
    )
    scoped, foreign = drop_foreign_runs(
        TranscriptEvidence(validation_runs=(run,)),
        (TEST_PATH,),
        str(repo),
        (TEST_PATH, source),
        candidate,
    )
    accepted = copy in {"matching", "different-regression"}
    retained = accepted or location == "effective_cwd"
    assert scoped.validation_runs == ((run,) if retained else ())
    assert foreign == ([] if retained else [run.command])
    gate = evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=(run,)),
        has_attributed_edits=True,
        changed_paths=(TEST_PATH, source),
        close_root=str(repo),
        candidate_commit_sha=sha,
    )
    assert any(item["command"] == run.command for item in gate.details["latest_runs"]) is retained
    assert bool(gate.details["foreign_scope_runs"]) is not retained
    # Cwd-based runs stay visible as diagnostics; candidate bytes decide credit.
    assert (TEST_PATH not in gate.details["pytest_uncovered_paths"]) is accepted
    assert (
        TEST_PATH in candidate_pytest_paths(run, (TEST_PATH,), (TEST_PATH, source), candidate)
    ) is accepted
    if accepted:
        assert candidate_pytest_paths(
            run, (TEST_PATH, regression), (TEST_PATH, source), candidate
        ) == frozenset((TEST_PATH, regression) if copy == "matching" else (TEST_PATH,))


def _edit(path: str, order: int) -> TranscriptEdit:
    return TranscriptEdit(
        session_id="earlier",
        source="claude",
        path=path,
        timestamp=START + timedelta(seconds=order),
        order=order,
        tool_name="Write",
    )


def _run(order: int, *, red: bool, output: str | None = None) -> TranscriptValidationRun:
    return TranscriptValidationRun(
        session_id="earlier" if red else "closing",
        source="claude",
        command=f"uv run pytest {TEST_PATH} -q",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure" if red else "success",
        started_at=START + timedelta(seconds=order),
        completed_at=START + timedelta(seconds=order, milliseconds=500),
        order=order,
        exit_code=1 if red else 0,
        output=output
        or (
            "________________ test_feature ________________\n"
            "E   assert 1 == 2\n"
            "tests/test_feature.py:4: AssertionError\n"
            "FAILED tests/test_feature.py::test_feature - assert 1 == 2\n"
            if red
            else "1 passed in 0.01s"
        ),
    )


def _test(path: str = TEST_PATH, symbol: str = "test_feature") -> AcceptanceTest:
    return AcceptanceTest(reference=f"{path}::{symbol}", path=path, symbol=symbol, body="")


async def _collect(tmp_path: Path, exclusion: str | None = None) -> TranscriptEvidence:
    """Fake only provider I/O; exercise the real claim filtering and evidence merge."""
    root = str(tmp_path.resolve())
    paths = {TEST_PATH, "src/feature.py"}
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    claim = START + timedelta(seconds=5 if exclusion == "before_claim" else 0)
    closing_claim = START + timedelta(seconds=10)

    def link(session: str, at: datetime) -> dict[str, Any]:
        return {
            "session_id": session,
            "task_id": "task",
            "action": "claimed",
            "created_at": at.isoformat(),
        }

    links = [link("closing", closing_claim)]
    if exclusion != "unrelated_session":
        links.append(link("earlier", claim))
    ctx = MagicMock()
    ctx.config = None
    ctx.session_task_manager.get_task_sessions.return_value = links
    sessions = {
        name: SimpleNamespace(id=name, source="claude", created_at=START)
        for name in ("earlier", "closing")
    }
    ctx.session_manager.get.side_effect = sessions.get
    ctx.session_var_manager.get_variables.return_value = {
        "task_edited_file_checkouts": {"task": {root: sorted(paths)}},
        "task_edited_file_checkouts_history_started_at": START.isoformat(),
    }

    def session_links(session: str) -> list[dict[str, Any]]:
        rows = [
            {
                "task": SimpleNamespace(id="task", closed_at=None),
                "action": "claimed",
                "link_created_at": (closing_claim if session == "closing" else claim).isoformat(),
            }
        ]
        if session == "earlier" and exclusion == "other_task":
            rows.insert(
                0,
                {
                    "task": SimpleNamespace(id="other", closed_at=None),
                    "action": "claimed",
                    "link_created_at": (START + timedelta(seconds=1)).isoformat(),
                },
            )
        return rows

    ctx.session_task_manager.get_session_tasks.side_effect = session_links

    async def parse(
        session: Any, window_start: str | datetime | None, *args: Any, **kwargs: Any
    ) -> TranscriptEvidence:
        # The provider reader's contract is to exclude records before its window.
        start = (
            datetime.fromisoformat(window_start) if isinstance(window_start, str) else window_start
        )
        original = TranscriptEvidence(
            edits=(_edit(TEST_PATH, 2), _edit("src/feature.py", 4)),
            validation_runs=(_run(3, red=True),),
            sessions=("earlier",),
        )
        if session.id == "closing":
            return TranscriptEvidence(validation_runs=(_run(11, red=False),), sessions=("closing",))
        return replace(
            original,
            edits=tuple(
                edit for edit in original.edits if start is None or edit.timestamp >= start
            ),
            validation_runs=tuple(
                run for run in original.validation_runs if start is None or run.started_at >= start
            ),
        )

    with (
        patch(f"{SUPPORT}.resolve_validation_detection_config"),
        patch(f"{SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{SUPPORT}.derive_transcript_evidence", new=AsyncMock(side_effect=parse)),
        patch(f"{SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        return await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id="closing",
            closing_session_id="closing",
            owner_window_start=closing_claim.isoformat(),
            task_edited_files=paths,
            repo_path=root,
        )


@pytest.mark.asyncio
async def test_cycle_credited_across_claim_continuity_chain(tmp_path: Path) -> None:
    evidence = await _collect(tmp_path)
    result = evaluate_tdd_evidence((_test(),), evidence)
    assert result.passed, result.findings
    assert result.red_runs == (f"uv run pytest {TEST_PATH} -q",)
    assert {run.session_id for run in evidence.validation_runs} == {"earlier", "closing"}


@pytest.mark.parametrize(
    "proof", ["current_claim", "later_edit", "earlier_claim", "foreign_checkout", "other_task"]
)
async def test_legacy_owner_history_requires_current_claim_transcript_edit(
    tmp_path: Path, proof: str
) -> None:
    root = str(tmp_path.resolve())
    paths = {TEST_PATH, "src/feature.py"}
    edit_root = tmp_path / "foreign" if proof == "foreign_checkout" else tmp_path
    records = [
        *_pair(2, "Write", {"file_path": str(edit_root / TEST_PATH), "content": "test"}, "ok"),
        *_pair(
            3,
            "Bash",
            {"command": f"uv run pytest {TEST_PATH} -q", "cwd": root},
            "________________ test_feature ________________\nE   assert 1 == 2\n"
            "tests/test_feature.py:4: AssertionError\n"
            "FAILED tests/test_feature.py::test_feature - assert 1 == 2\n",
        ),
        *_pair(
            4, "Write", {"file_path": str(edit_root / "src/feature.py"), "content": "code"}, "ok"
        ),
        *_pair(
            5,
            "Bash",
            {"command": f"uv run pytest {TEST_PATH} -q", "cwd": root},
            "1 passed in 0.01s\n",
        ),
    ]
    if proof == "later_edit":
        records.extend(
            _pair(
                6,
                "Write",
                {"file_path": str(tmp_path / "src/feature.py"), "content": "later"},
                "ok",
            )
        )
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    session = Session(
        id="owner",
        external_id="legacy-owner",
        machine_id=require_machine_id(),
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=START,
        updated_at=START,
    )
    variables: dict[str, Any] = {
        "task_edited_file_checkouts_history": {"task": {root: sorted(paths)}},
        "task_edited_file_checkouts_history_started_at": START.timestamp(),
    }
    if proof == "later_edit":
        variables["task_edited_file_checkouts_history"]["task"][str(tmp_path / "other")] = sorted(
            paths
        )
        variables["task_edited_file_times"] = {
            "task": {path: (START + timedelta(seconds=6)).timestamp() for path in paths}
        }
    if proof == "other_task":
        variables["task_edited_file_checkouts"] = {"other": {root: sorted(paths)}}
    window = START + timedelta(seconds=20) if proof == "earlier_claim" else START
    ctx = MagicMock()
    ctx.config = None
    ctx.session_manager.get.return_value = session
    ctx.session_var_manager.get_variables.return_value = variables
    ctx.session_task_manager.get_task_sessions.return_value = []
    ctx.session_task_manager.get_session_tasks.return_value = [
        {
            "task": SimpleNamespace(id="task", closed_at=None),
            "action": "claimed",
            "link_created_at": window.isoformat(),
        }
    ]
    with (
        patch(f"{SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id="owner",
            closing_session_id="owner",
            owner_window_start=window.isoformat(),
            task_edited_files=set(),
            repo_path=root,
        )
    assert {edit.path for edit in evidence.edits} == (
        paths if proof in {"current_claim", "later_edit"} else set()
    )
    if proof == "current_claim":
        result = evaluate_tdd_evidence((_test(),), evidence)
        assert result.passed, result.findings
    if proof in {"current_claim", "later_edit"}:
        from gobby.tasks.close_checklist import evaluate_validation_commands

        gate = evaluate_validation_commands(
            task_category="code",
            evidence=evidence,
            has_attributed_edits=True,
            changed_paths=("src/feature.py",),
            close_root=root,
        )
        assert gate.passed is (proof == "current_claim"), gate.message
        if proof == "later_edit":
            assert any(edit.timestamp == START + timedelta(seconds=6) for edit in evidence.edits)
            assert gate.details["latest_runs"] == []


@pytest.mark.parametrize("exclusion", [None, "other_task", "foreign_checkout"])
async def test_owner_history_paths_survive_live_projection_reset(
    tmp_path: Path, exclusion: str | None
) -> None:
    """Historical owner pairs must reach both halves of the real edit matcher."""
    root = str(tmp_path.resolve())
    recent = "docs/recent.md"
    paths = {TEST_PATH, "src/feature.py"}
    for path in paths | {recent}:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    history_root = str(tmp_path / "foreign") if exclusion == "foreign_checkout" else root
    variables: dict[str, Any] = {
        "task_edited_file_checkouts": {"task": {root: [recent]}},
        "task_edited_file_checkouts_history": {"task": {history_root: sorted(paths)}},
        "task_edited_file_checkouts_history_started_at": START.isoformat(),
        "task_edited_file_times": {
            "task": {path: START.timestamp() + 1 for path in paths | {recent}}
        },
    }
    if exclusion == "other_task":
        variables["task_edited_file_checkouts"]["other"] = {root: sorted(paths)}
    ctx = MagicMock()
    ctx.config = None
    ctx.session_manager.get.return_value = SimpleNamespace(
        id="owner", source="claude", created_at=START
    )
    ctx.session_var_manager.get_variables.return_value = variables
    ctx.session_task_manager.get_task_sessions.return_value = []
    ctx.session_task_manager.get_session_tasks.return_value = [
        {
            "task": SimpleNamespace(id="task", closed_at=None),
            "action": "claimed",
            "link_created_at": START.isoformat(),
        }
    ]

    async def parse(
        session: Any,
        window_start: Any,
        detection: Any,
        files: set[str],
        repo_path: str,
        *,
        task_checkout_paths: frozenset[tuple[str, str]],
        **kwargs: Any,
    ) -> TranscriptEvidence:
        original = (_edit(TEST_PATH, 2), _edit("src/feature.py", 4), _edit(recent, 10))
        return TranscriptEvidence(
            edits=tuple(
                edit
                for edit in original
                if match_task_file(str(tmp_path / edit.path), files, repo_path, task_checkout_paths)
            ),
            validation_runs=(_run(3, red=True), _run(11, red=False)),
        )

    with (
        patch(f"{SUPPORT}.resolve_validation_detection_config"),
        patch(f"{SUPPORT}.transcript_sync_point", return_value=None),
        patch(f"{SUPPORT}.derive_transcript_evidence", new=AsyncMock(side_effect=parse)),
        patch(f"{SUPPORT}.derive_prelink_runs", new=AsyncMock(return_value=())),
    ):
        evidence = await derive_close_transcript_evidence(
            ctx,
            task_id="task",
            owner_session_id="owner",
            closing_session_id="owner",
            owner_window_start=START.isoformat(),
            task_edited_files={recent},
            repo_path=root,
        )
    assert {edit.path for edit in evidence.edits} == (
        paths | {recent} if exclusion is None else {recent}
    )
    if exclusion is None:
        result = evaluate_tdd_evidence((_test(),), evidence)
        assert result.passed, result.findings


@pytest.mark.asyncio
@pytest.mark.parametrize("exclusion", ["unrelated_session", "other_task", "before_claim"])
async def test_cycle_not_credited_outside_continuity_chain(tmp_path: Path, exclusion: str) -> None:
    from gobby.tasks.close_checklist import evaluate_validation_commands

    evidence = await _collect(tmp_path, exclusion)
    assert evidence.validation_runs
    assert all(run.outcome == "success" for run in evidence.validation_runs)
    gate = evaluate_validation_commands(
        task_category="code", evidence=evidence, has_attributed_edits=True
    )
    assert gate.details["latest_runs"]
    assert all(run["outcome"] == "success" for run in gate.details["latest_runs"])
    result = evaluate_tdd_evidence((_test(),), evidence)
    assert not result.passed
    assert result.red_runs == ()
    assert result.findings == (
        f"{TEST_PATH}::test_feature: transcript has no edit of the named test",
    )


@pytest.mark.parametrize("runner", ["pytest", "pytest_with_sibling_location", "nextest"])
def test_compacted_red_credits_nodes_named_in_fail_summary(runner: str) -> None:
    is_pytest = runner.startswith("pytest")
    path = TEST_PATH if is_pytest else "crates/feature/tests/golden.rs"
    symbol = "test_feature" if is_pytest else "replays_corpus"
    command = (
        f"uv run pytest {path} -q" if is_pytest else "cargo nextest run -p feature --test golden"
    )
    output = (
        f"FAILED {path}::{symbol} - assert 1 == 2\n2 failed\n"
        if is_pytest
        else f"---- {symbol} stdout ----\n"
        f"thread '{symbol}' panicked at 'assertion failed: left == right'\n"
        f"FAIL [ 0.001s] (2/2) feature::golden {symbol}\nSummary 2 tests run: 0 passed, 2 failed\n"
    )
    if runner == "pytest_with_sibling_location":
        output = (
            "______ test_sibling ______\nE   assert 1 == 2\n"
            "tests/test_feature.py:10: in test_sibling\n"
            "FAILED tests/test_feature.py::test_sibling - assert 1 == 2\n"
        ) + output
    red = replace(_run(3, red=True, output=output), command=command)
    green = replace(
        _run(5, red=False),
        command=command,
        output="1 passed" if is_pytest else f"PASS [ 0.001s] (1/1) feature::golden {symbol}",
    )
    evidence = TranscriptEvidence(
        edits=(_edit(path, 2), _edit("src/feature.py", 4)), validation_runs=(red, green)
    )
    result = evaluate_tdd_evidence((_test(path, symbol),), evidence)
    assert result.passed, result.findings
    assert result.red_runs == (command,)


@pytest.mark.parametrize(
    "output",
    [
        "E   assert 1 == 2\n2 failed\n",
        "FAILED tests/test_other.py::test_feature - assert 1 == 2\n",
        "FAILED tests/test_feature.py::test_feature_extra - assert 1 == 2\n",
        "tests/test_feature.py::test_feature PASSED\n2 failed\n",
        "E   assert 1 == 2\n"
        "FAILED tests/test_feature.py::test_feature - Failed: Timeout (>30.0s) from pytest-timeout.\n",
        "FAILED tests/test_feature.py::test_feature - ValueError: bad input\n1 failed\n",
        "FAILED tests/test_feature.py::test_feature - [XPASS(strict)] not yet\n1 failed\n",
        "FAILED tests/test_feature.py::test_feature\n1 failed\n",
        "FAILED tests/test_feature.py::test_sibling - assert 1 == 2\n"
        "FAILED tests/test_feature.py::test_feature\n2 failed\n",
        "________________ test_sibling ________________\n"
        "E   assert test_feature == 2\n"
        "FAILED tests/test_feature.py::test_feature\n2 failed\n",
        "E   assert 1 == 2\n[first failure only; remaining details compacted]\n"
        "FAILED tests/test_feature.py::test_feature\n2 failed\n",
        "FAILED tests/test_feature.py::test_feature\nE   assert 1 == 2\n"
        "tests/test_other.py:10: in test_sibling\n",
    ],
)
def test_compacted_red_without_matching_node_is_rejected(output: str) -> None:
    evidence = TranscriptEvidence(
        edits=(_edit(TEST_PATH, 2), _edit("src/feature.py", 4)),
        validation_runs=(_run(3, red=True, output=output), _run(5, red=False)),
    )
    result = evaluate_tdd_evidence((_test(),), evidence)
    assert not result.passed
    assert result.red_runs == ()
    assert any("no attributable failure section" in finding for finding in result.findings)


@pytest.mark.parametrize(
    "output",
    [
        "FAIL [ 0.001s] (1/1) feature::golden replays_corpus\n",
        "---- test_sibling stdout ----\nthread 'test_sibling' panicked at 'assertion failed'\n"
        "FAIL [ 0.001s] (2/2) feature::golden replays_corpus\n",
        "---- test_sibling stdout ----\n"
        "thread 'test_sibling' panicked at 'assertion failed: replays_corpus'\n"
        "FAIL [ 0.001s] (2/2) feature::golden replays_corpus\n",
        "FAIL feature::golden replays_corpus\n"
        "thread 'test_sibling' panicked at 'assertion failed'\n",
        "---- replays_corpus stdout ----\n"
        "thread 'test_sibling' panicked at 'assertion failed'\n"
        "FAIL [ 0.001s] (2/2) feature::golden replays_corpus\n",
    ],
)
def test_nextest_summary_without_attributable_failure_is_rejected(output: str) -> None:
    path = "crates/feature/tests/golden.rs"
    command = "cargo nextest run -p feature --test golden"
    evidence = TranscriptEvidence(
        edits=(_edit(path, 2), _edit("crates/feature/src/lib.rs", 4)),
        validation_runs=(
            replace(_run(3, red=True, output=output), command=command),
            replace(
                _run(5, red=False),
                command=command,
                output="PASS [ 0.001s] (1/1) feature::golden replays_corpus",
            ),
        ),
    )
    result = evaluate_tdd_evidence((_test(path, "replays_corpus"),), evidence)
    assert not result.passed
    assert result.red_runs == ()


def _provider_evidence(
    tmp_path: Path, records: list[dict[str, Any]], files: set[str]
) -> TranscriptEvidence:
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    session = Session(
        id="earlier",
        external_id="fixture",
        machine_id="isolated",
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=START,
        updated_at=START,
    )
    evidence, _ = _derive_transcript_path_evidence(
        session,
        str(transcript),
        START,
        default_validation_detection_config(),
        files,
        str(tmp_path),
        frozenset((str(tmp_path), path) for path in files),
        [str(transcript)],
        resume=None,
    )
    return evidence


def _pair(
    step: int, name: str, arguments: dict[str, Any], result: str, *, cwd: str | None = None
) -> list[dict[str, Any]]:
    call = f"call-{step}"
    return [
        {
            "type": "assistant",
            "timestamp": (START + timedelta(seconds=step)).isoformat(),
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call, "name": name, "input": arguments}],
            },
        },
        {
            "type": "user",
            "timestamp": (START + timedelta(seconds=step, milliseconds=500)).isoformat(),
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call, "content": result}],
            },
        },
    ]


def _archived_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    archive_owner: str = "closing",
    window_start: datetime = START,
    overlap: bool = False,
    archive_mtime: datetime | None = None,
) -> TranscriptEvidence:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
    production = "src/feature.py"
    early = _pair(0, "Bash", {"command": "git status --short"}, "", cwd=str(tmp_path))
    early += _pair(
        1,
        "Write",
        {"file_path": str(tmp_path / TEST_PATH), "content": "def test_feature(): assert 1 == 2"},
        "",
        cwd=str(tmp_path),
    )
    early += _pair(
        2, "Bash", {"command": _run(2, red=True).command}, _run(2, red=True).output or ""
    )
    current = _pair(
        3,
        "Write",
        {"file_path": str(tmp_path / production), "content": "value = 2"},
        "",
        cwd=str(tmp_path),
    )
    current += _pair(4, "Bash", {"command": _run(4, red=False).command}, "1 passed in 0.01s")
    transcript = tmp_path / "current.jsonl"
    records = [*early, *current] if overlap else current
    transcript.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    archives = tmp_path / "archives"
    archives.mkdir()
    with gzip.open(archives / f"{archive_owner}.jsonl.gz", "wt", encoding="utf-8") as stream:
        stream.write("\n".join(json.dumps(row) for row in early) + "\n")
    if archive_mtime is not None:
        stamp = archive_mtime.timestamp()
        os.utime(archives / f"{archive_owner}.jsonl.gz", (stamp, stamp))
    session = Session(
        id="closing",
        external_id="closing",
        machine_id="isolated",
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=START,
        updated_at=START,
    )
    files = {TEST_PATH, production}
    evidence, _ = _derive_transcript_evidence_sync(
        session,
        window_start,
        default_validation_detection_config(),
        files,
        str(tmp_path),
        frozenset((str(tmp_path), path) for path in files),
        str(archives),
        "isolated",
        None,
    )
    return evidence


@pytest.mark.parametrize("overlap", [False, True])
def test_red_in_prior_transcript_of_closing_session_is_credited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlap: bool
) -> None:
    evidence = _archived_cycle(tmp_path, monkeypatch, overlap=overlap)
    result = evaluate_tdd_evidence((_test(),), evidence, implementation_paths=(TEST_PATH,))
    assert result.passed, result.findings
    assert result.red_runs == (_run(2, red=True).command,)
    assert evidence.sessions == ("closing",)
    assert str(tmp_path / "archives/closing.jsonl.gz") in evidence.attempted_paths
    assert len(evidence.validation_runs) == 2
    assert len(evidence.command_runs) == 1
    assert len(evidence.edits) == 2


def test_archive_written_before_claim_is_not_decompressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with patch(
        "gobby.tasks.transcript_evidence._iter_archive_lines",
        side_effect=AssertionError("pre-window archive must not be decompressed"),
    ) as archive_read:
        evidence = _archived_cycle(
            tmp_path, monkeypatch, archive_mtime=START - timedelta(seconds=1)
        )
    archive_read.assert_not_called()
    assert len(evidence.validation_runs) == 1
    assert not any(run.outcome == "failure" for run in evidence.validation_runs)


@pytest.mark.parametrize("exclusion", ["unrelated", "before_claim"])
def test_red_in_unrelated_session_transcript_is_not_credited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exclusion: str
) -> None:
    evidence = _archived_cycle(
        tmp_path,
        monkeypatch,
        archive_owner="other" if exclusion == "unrelated" else "closing",
        window_start=START + timedelta(seconds=3) if exclusion == "before_claim" else START,
    )
    result = evaluate_tdd_evidence((_test(),), evidence, implementation_paths=(TEST_PATH,))
    assert not result.passed
    assert not result.red_runs
    assert not any(run.outcome == "failure" for run in evidence.validation_runs)


@pytest.mark.parametrize("has_red", [False, True])
def test_named_unchanged_test_is_not_refused_for_missing_edit(has_red: bool) -> None:
    evidence = TranscriptEvidence(
        edits=(_edit("src/feature.py", 3),),
        validation_runs=(*((_run(2, red=True),) if has_red else ()), _run(4, red=False)),
    )
    result = evaluate_tdd_evidence((_test(),), evidence, implementation_paths=("src/feature.py",))
    assert result.passed is has_red, result.findings
    assert not any("no edit of the named test" in finding for finding in result.findings)
    assert result.red_runs == ((_run(2, red=True).command,) if has_red else ())
    assert result.details()["regression_tests"] == ([] if has_red else [_test().reference])


def test_unchanged_regression_can_accompany_another_unchanged_tests_cycle() -> None:
    regression = _test("tests/test_other.py", "test_other")
    evidence = TranscriptEvidence(
        edits=(_edit("src/feature.py", 3),),
        validation_runs=(
            _run(2, red=True),
            _run(4, red=False),
            replace(_run(5, red=False), command="uv run pytest tests/test_other.py -q"),
        ),
    )
    result = evaluate_tdd_evidence(
        (_test(), regression), evidence, implementation_paths=("src/feature.py",)
    )
    assert result.passed, result.findings
    assert result.regression_tests == (regression.reference,)


def test_unchanged_regression_still_requires_a_fresh_pass() -> None:
    evidence = TranscriptEvidence(
        edits=(_edit("src/feature.py", 3),),
        validation_runs=(_run(2, red=False),),
    )
    result = evaluate_tdd_evidence((_test(),), evidence, implementation_paths=("src/feature.py",))
    assert not result.passed
    assert any("no pass" in finding for finding in result.findings)


def test_unchanged_regression_requires_a_visible_production_edit() -> None:
    evidence = TranscriptEvidence(validation_runs=(_run(4, red=False),))
    result = evaluate_tdd_evidence((_test(),), evidence, implementation_paths=("src/feature.py",))
    assert not result.passed
    assert any("production edit" in finding for finding in result.findings)


def test_unchanged_red_does_not_replace_changed_tests_red() -> None:
    changed = _test("tests/test_changed.py", "test_changed")
    evidence = TranscriptEvidence(
        edits=(_edit(changed.path, 1), _edit("src/feature.py", 3)),
        validation_runs=(
            _run(2, red=True),
            _run(4, red=False),
            replace(_run(5, red=False), command="uv run pytest tests/test_changed.py -q"),
        ),
    )
    result = evaluate_tdd_evidence(
        (changed, _test()), evidence, implementation_paths=(changed.path, "src/feature.py")
    )
    assert not result.passed
    assert not result.red_runs
    assert any(changed.reference in finding for finding in result.findings)


@pytest.mark.parametrize(
    "symbol",
    [
        "test_checkout_registration_uses_key_machine",
        "test_checkout_registration_refusals",
        "test_report_inbox_replaces_snapshot_and_merges_exits",
        "test_node_routes_refuse_unverified_and_local_identity",
        "test_report_order_and_epoch_barrier",
        "test_reported_workspace_is_durable_before_200",
        "test_reported_workspace_moves_updated_at",
        "test_report_byte_budgets",
        "test_checkout_body_is_capped_before_parse_or_write",
    ],
)
@pytest.mark.parametrize(
    "failure_kind",
    [
        "stub",
        "assertion_mentions_stub",
        "sibling_stub",
        "no_stub_frame",
        "foreign_stub_frame",
        "captured_stdout",
        "captured_stderr",
        "captured_log",
        "source_no_stub",
        "source_other_function",
        "source_wrong_line",
        "source_unconfirmed",
        "source_late_confirmation",
        "source_overwritten",
        "fragment_stub",
        "fragment_wrong_function",
        "fragment_no_stub",
        "fragment_unconfirmed",
        "source_docstring",
        "fragment_docstring",
        "later_after_capture",
        "forged_later_header",
        "forged_duplicate_header",
        "forged_header_and_summary",
        "auto_stub",
        "auto_fragment",
        "auto_wrong_line",
        "auto_source_no_stub",
        "auto_fragment_wrong_function",
        "showlocals_short",
        "showlocals_long",
        "showlocals_combined",
        "showlocals_config",
        "showlocals_env",
        "showlocals_header_config",
        "showlocals_header_env",
    ],
)
def test_node_route_postproduction_not_implemented_is_attributed(
    symbol: str, failure_kind: str
) -> None:
    test = _test("tests/servers/test_node_routes.py", symbol)
    production = "src/gobby/servers/routes/nodes.py"
    # Observed #24270 shape: --tb=short-style HTTP frames, no FAILED summary.
    output = (
        f"______________ {symbol} ______________\n"
        f"{test.path}:612: in {symbol}\n"
        '    valid_too_large = _post(client, "checkouts", oversized)\n'
        f"{test.path}:209: in _post\n"
        '    response = client.post(f"/api/nodes/{route}", headers=_headers(machine), json=body)\n'
        ".venv/lib/python3.14/site-packages/starlette/testclient.py:356: in handle_request\n"
        "    raise exc\n"
        f"{production}:117: in register_checkout\n"
        '    raise NotImplementedError("stub: checkout registration route")\n'
    )
    output += (
        "E   NotImplementedError: stub: checkout registration route\n"
        if failure_kind in {"stub", "no_stub_frame", "foreign_stub_frame"}
        else "E   AssertionError: NotImplementedError was mentioned but not raised\n"
    )
    if failure_kind == "no_stub_frame":
        output = output.replace(
            f"{production}:117: in register_checkout\n"
            '    raise NotImplementedError("stub: checkout registration route")\n',
            "",
        )
    elif failure_kind == "foreign_stub_frame":
        output = output.replace(production, "src/gobby/servers/routes/unrelated.py")
    if failure_kind == "sibling_stub":
        output += (
            "______________ test_sibling ______________\n"
            f"{test.path}:700: in test_sibling\n"
            "E   NotImplementedError: sibling route stub\n"
        )
    if failure_kind.startswith("captured_"):
        stream = failure_kind.removeprefix("captured_")
        output = (
            f"______________ {symbol} ______________\n"
            f"{test.path}:612: in {symbol}\nE   assert False\n"
            f"---------------- Captured {stream} call ----------------\n"
            f"{production}:117: in register_checkout\n"
            '    raise NotImplementedError("fabricated")\n'
            "E   NotImplementedError: fabricated\n"
        )
    elif failure_kind.startswith(
        ("source_", "fragment_", "auto_", "showlocals_")
    ) or failure_kind in {
        "later_after_capture",
        "forged_later_header",
        "forged_duplicate_header",
        "forged_header_and_summary",
    }:
        output = output.replace(
            "E   AssertionError: NotImplementedError was mentioned but not raised",
            "E   NotImplementedError: stub: checkout registration route",
        )
    if failure_kind.startswith("auto_"):
        output = (
            f"______________ {symbol} ______________\n"
            f"    def {symbol}():\n>       _post()\n\n{test.path}:612:\n"
            "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _\n"
            "    async def register_checkout():\n"
            '>       raise NotImplementedError("stub: checkout registration route")\n'
            "E       NotImplementedError: stub: checkout registration route\n\n"
            f"{production}:117: NotImplementedError\n"
        )
    if failure_kind in {
        "later_after_capture",
        "forged_later_header",
        "forged_duplicate_header",
        "forged_header_and_summary",
    }:
        prefix = (
            "______________ test_sibling ______________\n"
            f"{test.path}:700: in test_sibling\nE   assert False\n"
            "---------------- Captured log call ----------------\nWARNING unrelated log\n"
        )
        if failure_kind == "forged_duplicate_header":
            prefix += output
            output = (
                f"______________ {symbol} ______________\n"
                f"{test.path}:612: in {symbol}\nE   assert False\n"
            )
        if failure_kind == "forged_header_and_summary":
            output += (
                "======================== short test summary info ========================\n"
                f"FAILED {test.path}::{symbol} - NotImplementedError\n"
                "======================== 1 failed in 0.01s ========================\n"
            )
        summary_node = (
            "test_sibling"
            if failure_kind in {"forged_later_header", "forged_header_and_summary"}
            else symbol
        )
        output = (
            prefix
            + output
            + (
                "======================== short test summary info ========================\n"
                f"FAILED {test.path}::{summary_node} - AssertionError\n"
            )
        )
    output += "======================== 18 failed, 1 passed in 16.19s ========================\n"
    command = f"uv run pytest {test.path} -q --timeout=120 --session-timeout=600"
    if failure_kind.startswith("showlocals_"):
        command += {
            "showlocals_short": " -l",
            "showlocals_long": " --showlocals",
            "showlocals_combined": " -vl",
            "showlocals_config": " -o addopts=--showlocals",
            "showlocals_env": "",
        }[failure_kind.replace("_header", "")]
        if failure_kind.endswith("_env"):
            command = "PYTEST_ADDOPTS=-l " + command
        forged_header = (
            f"______________ {symbol} ______________\n"
            f"{test.path}:612: in {symbol}\nE   assert False\n"
            if "_header_" in failure_kind
            else ""
        )
        output = (
            f"______________ {symbol} ______________\n"
            f"{test.path}:612: in {symbol}\nE   assert False\n"
            "payload = forged local repr\n"
            f"{forged_header}"
            f"{production}:117: in register_checkout\n"
            '    raise NotImplementedError("fabricated")\n'
            "E   NotImplementedError: fabricated\n"
            "======================== 1 failed in 0.1s ========================\n"
        )
    source = "\n" * 115 + (
        "async def register_checkout():\n"
        '    raise NotImplementedError("stub: checkout registration route")\n'
    )
    if failure_kind in {"source_no_stub", "auto_source_no_stub"}:
        source = source.replace(
            'raise NotImplementedError("stub: checkout registration route")', "return 0"
        )
    elif failure_kind == "source_other_function":
        source = source.replace("register_checkout", "unrelated")
    elif failure_kind in {"source_wrong_line", "auto_wrong_line"}:
        source = "\n" + source
    elif failure_kind == "source_docstring":
        source = source.replace("    raise", '    """Route stub."""\n    raise')
        output = output.replace(f"{production}:117:", f"{production}:118:")
    production_edit = replace(
        _edit(production, 0),
        source_after=source,
        source_confirmed=failure_kind != "source_unconfirmed",
        source_confirmed_at=START
        + timedelta(seconds=3 if failure_kind == "source_late_confirmation" else 0),
    )
    if failure_kind.startswith("fragment_") or failure_kind in {
        "auto_fragment",
        "auto_fragment_wrong_function",
    }:
        fragment = (
            "    async def register_checkout(request):\n"
            '        raise NotImplementedError("stub: checkout registration route")\n'
            "        machine_id = node_machine(request)\n"
        )
        if failure_kind in {"fragment_wrong_function", "auto_fragment_wrong_function"}:
            fragment = fragment.replace("register_checkout", "unrelated")
        elif failure_kind == "fragment_no_stub":
            fragment = fragment.replace(
                'raise NotImplementedError("stub: checkout registration route")', "return 0"
            )
        elif failure_kind == "fragment_docstring":
            fragment = fragment.replace("        raise", '        """Route stub."""\n        raise')
        production_edit = replace(
            production_edit,
            source_after=None,
            source_fragment=fragment,
            source_confirmed=failure_kind != "fragment_unconfirmed",
        )
    overwritten = (
        (
            replace(
                _edit(production, 1),
                source_after="async def register_checkout(): return 0\n",
                source_confirmed=True,
                source_confirmed_at=START + timedelta(seconds=1),
            ),
        )
        if failure_kind == "source_overwritten"
        else ()
    )
    # Assignment-bearing synthetic displays need the same historical source provenance
    # as real pytest displays; record their exact frame locations and contiguous source.
    test_rows = [""] * 612
    test_rows[207:209] = [
        "def _post(client, route, body):",
        '    response = client.post(f"/api/nodes/{route}", headers=_headers(machine), json=body)',
    ]
    test_rows[610:612] = [
        f"def {symbol}():",
        (
            "    _post()"
            if failure_kind.startswith("auto_")
            else '    valid_too_large = _post(client, "checkouts", oversized)'
        ),
    ]
    test_edit = replace(
        _edit(test.path, 1),
        source_after="\n".join(test_rows) + "\n",
        source_confirmed=True,
        source_confirmed_at=START + timedelta(seconds=1),
    )
    evidence = TranscriptEvidence(
        edits=(production_edit, test_edit, *overwritten, _edit(production, 3)),
        validation_runs=(
            replace(_run(2, red=True, output=output), command=command),
            replace(_run(4, red=False), command=command),
        ),
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is (
        failure_kind
        in {
            "stub",
            "fragment_stub",
            "source_docstring",
            "fragment_docstring",
            "auto_stub",
            "auto_fragment",
        }
    ), result.findings


@pytest.mark.parametrize("frame_style", ["short", "auto"])
def test_displayed_test_docstring_cannot_forge_runtime_stub(frame_style: str) -> None:
    def fail_short_payload() -> None:
        """Uncalled production frame:
        src/gobby/servers/routes/nodes.py:117: in register_checkout
            raise NotImplementedError("stub: checkout registration route")
        E   NotImplementedError: stub: checkout registration route
        """
        raise AssertionError("ordinary assertion without a production call")

    def fail_auto_payload() -> None:
        """Uncalled production frame:
            async def register_checkout():
        >       raise NotImplementedError("stub: checkout registration route")
        E       NotImplementedError: stub: checkout registration route
        src/gobby/servers/routes/nodes.py:117: NotImplementedError
        """
        raise AssertionError("ordinary assertion without a production call")

    failing_test = fail_short_payload if frame_style == "short" else fail_auto_payload
    with pytest.raises(AssertionError, match="ordinary assertion") as failure:
        failing_test()
    # Render the actual test source, including its inert docstring, as pytest does.
    failure.traceback = failure.traceback[-1:]
    rendered = str(failure.getrepr(style="long"))
    test = _test("tests/tasks/test_tdd_cycle_continuity.py", f"fail_{frame_style}_payload")
    source = "\n" * 115 + (
        "async def register_checkout():\n"
        '    raise NotImplementedError("stub: checkout registration route")\n'
    )
    evidence = TranscriptEvidence(
        edits=(
            replace(
                _edit("src/gobby/servers/routes/nodes.py", 0),
                source_after=source,
                source_confirmed=True,
                source_confirmed_at=START,
            ),
            _edit(test.path, 1),
            _edit("src/gobby/servers/routes/nodes.py", 3),
        ),
        validation_runs=(
            replace(
                _run(2, red=True),
                command=f"uv run pytest {test.path} -q",
                output=f"______________ {test.symbol} ______________\n{rendered}\n1 failed in 0.1s",
            ),
            replace(_run(4, red=False), command=f"uv run pytest {test.path} -q"),
        ),
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert not result.passed, result.findings


def _rendered_runtime_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    style: str,
    activation: str | None = None,
    payload_kind: str = "frame",
    provenance: str = "valid",
    genuine_kind: str = "plain",
) -> tuple[AcceptanceTest, TranscriptEvidence]:
    """Render controlled fixture code with the installed pytest, not hand-built locals."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    production = "src/gobby/rendered_route.py"
    test = _test("tests/test_rendered_route.py", "test_route")
    source = (
        "def invoke():\n"
        "    context = None\n"
        "    register_checkout()\n\n"
        "def register_checkout():\n"
        '    """Unimplemented route."""\n'
        '    raise NotImplementedError("stub")\n'
    )
    if genuine_kind == "argument":
        source = source.replace("register_checkout()", "register_checkout(None)", 1).replace(
            "def register_checkout():", "def register_checkout(request):"
        )
    elif genuine_kind == "method":
        test = _test(test.path, "TestRoutes.test_route")
        source = source.replace("register_checkout()", "Routes().register_checkout()", 1).replace(
            "def register_checkout():\n"
            '    """Unimplemented route."""\n'
            '    raise NotImplementedError("stub")\n',
            "class Routes:\n"
            "    def register_checkout(self):\n"
            '        """Unimplemented route."""\n'
            '        raise NotImplementedError("stub")\n',
        )
    if payload_kind in {"funcargs", "plain_funcargs"}:
        source = 'def register_checkout(evil):\n    raise NotImplementedError("stub")\n'
    forged = (
        f"{production}:7: in register_checkout\n"
        '    raise NotImplementedError("stub")\n'
        "E   NotImplementedError: stub\n"
    )
    if payload_kind == "header":
        forged = (
            "______________ test_route ______________\n"
            f"{test.path}:2: in test_route\n    result = invoke()\n" + forged
        )
    elif payload_kind == "locals_key":
        source += '\ndef dormant(param = None):\n    raise NotImplementedError("stub")\n'
        forged = (
            'None):\n>       raise NotImplementedError("stub")\n'
            "E       NotImplementedError: stub\n"
            f"{production}:10: NotImplementedError\n"
        )

    class Payload(Iterator[None]):
        def __repr__(self) -> str:
            if payload_kind == "plain_funcargs":
                return "Evil"
            if payload_kind == "funcargs":
                return "Evil\n\n" + forged + "\nevil = Evil"
            if payload_kind == "locals_key":
                return forged
            first_line = "Payload()" if payload_kind == "matching_assignment" else "forged local"
            return first_line + "\n" + forged

        def __next__(self) -> None:
            return None

    if payload_kind in {"funcargs", "plain_funcargs"}:
        test_source = "def test_route(evil):\n    assert False\n"
    elif activation is None:
        test_source = "def test_route():\n    result = invoke()\n"
        if genuine_kind == "fixture":
            test_source = test_source.replace("def test_route():", "def test_route(tmp_path):")
        elif genuine_kind == "method":
            test_source = (
                "class TestRoutes:\n    def test_route(self):\n        result = invoke()\n"
            )
        elif genuine_kind == "sibling":
            test_source += (
                "\ndef helper():\n    return 1\n\ndef test_another():\n    assert helper() == 2\n"
            )
    elif payload_kind == "locals_key":
        test_source = (
            "def test_route():\n    middle()\n\n"
            "def middle():\n    last()\n\n"
            'def last():\n    raise AssertionError("ordinary assertion")\n'
        )
    elif payload_kind == "nonword":
        test_source = (
            "def test_route():\n"
            "    next(last() for item in iterator)\n\n"
            "def last():\n"
            '    raise AssertionError("ordinary assertion")\n'
        )
    else:
        test_source = (
            "def test_route():\n"
            "    middle()\n\n"
            "def middle():\n"
            "    payload    = Payload()\n"
            "    last()\n\n"
            "def last():\n"
            '    raise AssertionError("ordinary assertion")\n'
        )
    namespace: dict[str, Any] = {"Payload": Payload, "iterator": Payload()}
    # Only controlled fixture modules execute here. The production evaluator receives
    # historical snapshots and must never import fixture/current checkout modules.
    for path, text in ((production, source), (test.path, test_source)):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        module = ast.parse(text, filename=str(file))
        if path == test.path:
            rewrite_asserts(module, text.encode(), str(file))
        exec(compile(module, str(file), "exec"), namespace)
    options = ["-vv"]
    if activation == "config":
        options += ["-o", "addopts=--showlocals"]
    elif activation == "env":
        monkeypatch.setenv("PYTEST_ADDOPTS", "--showlocals")
    # fromdictargs deliberately disables addopts; parse the actual CLI/env inputs.
    config = get_config(options)
    config.parse(options)
    showlocals = bool(config.getoption("showlocals"))
    assert showlocals is (activation is not None)
    if payload_kind in {"funcargs", "plain_funcargs"}:
        # Copy a real rendered raising frame into the repr, never paste source or
        # location rows into the exception output after rendering.
        with pytest.raises(NotImplementedError) as stub_failure:
            namespace["register_checkout"](None)
        stub_failure.traceback = stub_failure.traceback[1:]
        stub_terminal = StringIO()
        stub_failure.getrepr(style="long", funcargs=False).toterminal(
            TerminalWriter(file=stub_terminal)
        )
        forged = stub_terminal.getvalue()
    assertion_only = activation is not None or payload_kind in {"funcargs", "plain_funcargs"}
    with pytest.raises(AssertionError if assertion_only else NotImplementedError) as failure:
        if payload_kind in {"funcargs", "plain_funcargs"}:
            namespace["test_route"](Payload())
        elif genuine_kind == "method":
            namespace["TestRoutes"]().test_route()
        elif genuine_kind == "fixture":
            namespace["test_route"](tmp_path)
        else:
            namespace["test_route"]()
    traceback = failure.traceback[1:]
    assert len(traceback) == (1 if payload_kind in {"funcargs", "plain_funcargs"} else 3)
    if payload_kind == "locals_key":
        # Python 3.13+ exposes a writable frame-locals proxy. Pytest must render
        # this non-identifier key itself; no fabricated traceback row is inserted.
        traceback[1].frame.f_locals["    def dormant(param"] = Payload()
    if style == "auto" and len(traceback) == 3:
        traceback = type(traceback)(
            [traceback[0], traceback[1].with_repr_style("short"), traceback[2]]
        )
    failure.traceback = traceback
    representation = failure.getrepr(
        style="short" if style == "short" else "long",
        showlocals=showlocals,
        funcargs=True,
        truncate_locals=False,
    )
    # TerminalRepr.__str__ strips the first source line's prefix for serialization.
    # Preserve the exact terminal rendering used by pytest's normal CLI output.
    terminal_output = StringIO()
    representation.toterminal(TerminalWriter(file=terminal_output))
    rendered = terminal_output.getvalue()
    if payload_kind == "funcargs" and style == "auto":
        assert "Evil\n\n" + forged + "\nevil = Evil" in rendered
    sibling_output = ""
    if genuine_kind == "sibling":
        with pytest.raises(AssertionError) as sibling_failure:
            namespace["test_another"]()
        sibling_failure.traceback = sibling_failure.traceback[1:]
        sibling_representation = sibling_failure.getrepr(
            style="short" if style == "short" else "long", funcargs=True
        )
        sibling_terminal = StringIO()
        sibling_representation.toterminal(TerminalWriter(file=sibling_terminal))
        sibling_output = (
            "______________ test_another ______________\n" + sibling_terminal.getvalue() + "\n"
        )
        assert re.search(r"E[ \t]+\+[ \t]+where 1 = helper\(\)", sibling_output)
    if activation is not None:
        # First/last functions have no locals. Auto's sole payload is a short middle frame.
        local_name = {"nonword": ".0", "locals_key": "def dormant"}.get(payload_kind, "payload")
        assert local_name in rendered
        assert forged.strip() in rendered
    snapshot = test_source
    if provenance == "wrong_line":
        snapshot = snapshot.replace("result = invoke()", "result = other()")
    elif provenance == "wrong_context":
        snapshot = snapshot.replace("def test_route():", "def another_route():")
    elif provenance == "ambiguous":
        snapshot += "\ndef test_route():\n    result = invoke()\n"
    elif provenance == "wrong_location":
        rendered = rendered.replace(f"{test.path}:2:", f"{test.path}:1:")
    command = f"uv run pytest {test.path} -q -vv"
    if activation == "config":
        command += " -o addopts=--showlocals"
    elif activation == "env":
        command = "PYTEST_ADDOPTS=--showlocals " + command
    test_edit = replace(
        _edit(test.path, 1),
        source_after=None if provenance == "missing" else snapshot,
        source_confirmed=provenance != "unconfirmed",
        source_confirmed_at=START + timedelta(seconds=3 if provenance == "late" else 1),
    )
    evidence = TranscriptEvidence(
        edits=(
            replace(
                _edit(production, 0),
                source_after=source,
                source_confirmed=True,
                source_confirmed_at=START,
            ),
            test_edit,
            _edit(production, 3),
        ),
        validation_runs=(
            replace(
                _run(2, red=True),
                command=command,
                output=(
                    f"{sibling_output}______________ {test.symbol} ______________\n{rendered}\n"
                    f"{2 if sibling_output else 1} failed in 0.1s"
                ),
            ),
            replace(_run(4, red=False), command=command),
        ),
    )
    return test, evidence


@pytest.mark.parametrize("style", ["short", "auto"])
@pytest.mark.parametrize(
    "provenance",
    [
        "valid",
        "wrong_line",
        "wrong_context",
        "wrong_location",
        "missing",
        "unconfirmed",
        "ambiguous",
        "late",
    ],
)
def test_real_pytest_source_assignments_need_confirmed_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, style: str, provenance: str
) -> None:
    test, evidence = _rendered_runtime_case(tmp_path, monkeypatch, style, provenance=provenance)
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed is (provenance == "valid"), (
        result.findings,
        evidence.validation_runs[0].output,
    )


@pytest.mark.parametrize("style", ["short", "auto", "long"])
@pytest.mark.parametrize("activation", ["config", "env"])
@pytest.mark.parametrize("payload_kind", ["frame", "matching_assignment", "header", "nonword"])
def test_real_pytest_locals_cannot_forge_runtime_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    style: str,
    activation: str,
    payload_kind: str,
) -> None:
    test, evidence = _rendered_runtime_case(tmp_path, monkeypatch, style, activation, payload_kind)
    result = evaluate_tdd_evidence((test,), evidence)
    assert not result.passed, result.findings


@pytest.mark.parametrize("style", ["short", "auto"])
@pytest.mark.parametrize("genuine_kind", ["argument", "method", "fixture", "sibling"])
def test_real_pytest_renderer_metadata_preserves_runtime_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, style: str, genuine_kind: str
) -> None:
    test, evidence = _rendered_runtime_case(tmp_path, monkeypatch, style, genuine_kind=genuine_kind)
    result = evaluate_tdd_evidence((test,), evidence)
    assert result.passed, (result.findings, evidence.validation_runs[0].output)


@pytest.mark.parametrize("style", ["short", "auto"])
@pytest.mark.parametrize("payload_kind", ["funcargs", "plain_funcargs"])
def test_real_pytest_funcargs_cannot_forge_runtime_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, style: str, payload_kind: str
) -> None:
    test, evidence = _rendered_runtime_case(tmp_path, monkeypatch, style, payload_kind=payload_kind)
    result = evaluate_tdd_evidence((test,), evidence)
    assert not result.passed, (result.findings, evidence.validation_runs[0].output)


@pytest.mark.parametrize("style", ["short", "auto", "long"])
@pytest.mark.parametrize("activation", ["config", "env"])
def test_real_pytest_locals_key_cannot_forge_source_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, style: str, activation: str
) -> None:
    test, evidence = _rendered_runtime_case(
        tmp_path, monkeypatch, style, activation, payload_kind="locals_key"
    )
    result = evaluate_tdd_evidence((test,), evidence)
    assert not result.passed, (result.findings, evidence.validation_runs[0].output)


@pytest.mark.parametrize("provenance", ["matching", "foreign", "missing", "explicit_foreign"])
def test_relative_shell_edit_uses_recorded_checkout(tmp_path: Path, provenance: str) -> None:
    cwd = None if provenance == "missing" else str(tmp_path)
    if provenance == "foreign":
        cwd = str(tmp_path / "foreign")
    arguments = {"command": f"cat >> {TEST_PATH} <<'EOF'\ndef test_feature(): assert False\nEOF"}
    if provenance == "explicit_foreign":
        arguments["cwd"] = str(tmp_path / "foreign")
    evidence = _provider_evidence(tmp_path, _pair(1, "Bash", arguments, "", cwd=cwd), {TEST_PATH})
    assert tuple(edit.path for edit in evidence.edits) == (
        (TEST_PATH,) if provenance == "matching" else ()
    )


@pytest.mark.parametrize(
    "receipt", ["valid", "wrong_path", "wrong_id", "assistant", "no_exit", "wrong_description"]
)
def test_monitor_receipt_credits_original_background_cycle(tmp_path: Path, receipt: str) -> None:
    test_path = "crates/feature/tests/golden.rs"
    production = "crates/feature/src/lib.rs"
    output_path = str(tmp_path / "tasks/job.output")
    command = "cargo nextest run -p feature --test golden"
    records = _pair(
        1,
        "Write",
        {
            "file_path": str(tmp_path / test_path),
            "content": "#[test] fn replays_corpus() { assert!(false); }",
        },
        "",
    )
    records += _pair(
        2,
        "Bash",
        {"command": command, "run_in_background": True, "description": "RED tests"},
        "Command did not complete within its 600s timeout and was moved to the "
        f"background (ID: job). Output is being written to: {output_path}. "
        "You will be notified when it completes.",
    )
    monitor_path = output_path + ".other" if receipt == "wrong_path" else output_path
    records += _pair(
        3,
        "Monitor",
        {
            "command": f'tail -n +1 -f {monitor_path} | grep -E --line-buffered "Summary|PASS|FAIL|panicked|exited with code"'
        },
        "Monitor started (task monitor, expires in 30m unless the source ends first; you get one notice at expiry).",
    )
    monitor_id = "other" if receipt == "wrong_id" else "monitor"
    if receipt != "no_exit":
        description = "unrelated tests" if receipt == "wrong_description" else "RED tests"
        records.append(
            {
                "type": "user",
                "timestamp": (START + timedelta(seconds=3, milliseconds=900)).isoformat(),
                "message": {
                    "role": "user",
                    "content": f'<task-notification><task-id>job</task-id><tool-use-id>call-2</tool-use-id><output-file>{output_path}</output-file><status>failed</status><summary>Background command "{description}" failed with exit code 100</summary></task-notification>',
                },
            }
        )
    for step, text in [
        (
            4,
            "thread 'replays_corpus' panicked at 'assertion failed: left == right'\n"
            "FAIL [ 0.001s] (1/1) feature::golden replays_corpus\nSummary 1 tests run: 0 passed, 1 failed",
        ),
        (5, "[exited with code 100]"),
    ]:
        prompt = f'<task-notification><task-id>{monitor_id}</task-id><summary>Monitor event: "tests"</summary><event>{text}</event></task-notification>'
        record: dict[str, Any] = {
            "type": "attachment",
            "timestamp": (START + timedelta(seconds=step)).isoformat(),
            "attachment": {"type": "queued_command", "prompt": prompt},
        }
        if receipt == "assistant":
            record = {
                "type": "assistant",
                "timestamp": record["timestamp"],
                "message": {"role": "assistant", "content": prompt},
            }
        records.append(record)
    records += _pair(
        6, "Write", {"file_path": str(tmp_path / production), "content": "pub fn feature() {}"}, ""
    )
    records += _pair(
        7,
        "Bash",
        {"command": command},
        "PASS [ 0.001s] (1/1) feature::golden replays_corpus\nSummary 1 tests run: 1 passed, 0 failed",
    )
    evidence = _provider_evidence(tmp_path, records, {test_path, production})
    result = evaluate_tdd_evidence((_test(test_path, "replays_corpus"),), evidence)
    assert result.passed is (receipt == "valid"), result.findings
    original = evidence.validation_runs[0]
    assert original.provider_call_id == "call-2"
    assert original.outcome == (
        "unknown" if receipt in {"no_exit", "wrong_description"} else "failure"
    )
    if receipt == "valid":
        assert original.exit_code == 100
        assert "FAIL [ 0.001s] (1/1) feature::golden replays_corpus" in (original.output or "")
