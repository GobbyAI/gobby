"""Task-level TDD cycles survive handoff and compacted runner output."""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
from gobby.tasks.transcript_evidence import _derive_transcript_path_evidence
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
