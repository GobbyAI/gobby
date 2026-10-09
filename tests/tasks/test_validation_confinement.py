"""Confinement credit preserves candidate bytes and durable owner handoffs."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.tasks.close_test_coverage import CloseCandidate
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun
from gobby.tasks.validation_confinement import (
    ConfinementContext,
    ForeignValidationOwner,
    confinement_credit,
)

NOW = datetime(2026, 10, 9, tzinfo=UTC)
OWNED = ("tests/test_one.py", "tests/test_two.py")
FOREIGN = "tests/test_foreign.py"


def _run(root: Path, order: int, *, failed: bool = False) -> TranscriptValidationRun:
    targets = (*OWNED, FOREIGN) if failed else (OWNED[0],)
    return TranscriptValidationRun(
        session_id="developer",
        source="codex",
        command="uv run pytest " + " ".join(targets) + " -q",
        categories=("test",),
        matcher_id="pytest",
        label="pytest",
        outcome="failure" if failed else "success",
        exit_code=1 if failed else 0,
        started_at=NOW + timedelta(seconds=order),
        completed_at=NOW + timedelta(seconds=order + 1),
        order=order,
        workdir=str(root),
        output=(f"FAILED {FOREIGN}::test_bad - AssertionError\n1 failed, 2 passed in 1.0s")
        if failed
        else "1 passed in 1.0s",
    )


@pytest.fixture
def case(tmp_path: Path) -> tuple[CloseCandidate, ConfinementContext]:
    candidate = CloseCandidate(str(tmp_path), "candidate")
    blobs: dict[str, str] = {}
    for path in (*OWNED, FOREIGN):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        data = b"def test_ok():\n    assert True\n"
        file.write_bytes(data)
        blobs[path] = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    candidate.__dict__["blobs"] = blobs
    failed = _run(tmp_path, 1, failed=True)
    handoff = InterSessionMessage(
        id="handoff",
        from_session="developer",
        to_session="owner",
        content=f"Failing command: {failed.command}; {FOREIGN}: AssertionError; blocks close.",
        priority="normal",
        sent_at=NOW + timedelta(seconds=3),
    )
    return candidate, ConfinementContext(
        owners=(ForeignValidationOwner(FOREIGN, "owner", "foreign-task"),),
        handoffs=(handoff,),
    )


def _credit(
    candidate: CloseCandidate,
    context: ConfinementContext,
    *,
    failure: TranscriptValidationRun | None = None,
    green: TranscriptValidationRun | None = None,
) -> tuple[frozenset[str], list[dict[str, object]]]:
    root = Path(candidate.root)
    return confinement_credit(
        runs=(failure or _run(root, 1, failed=True), green or _run(root, 4)),
        required_paths=OWNED,
        changed_paths=OWNED,
        candidate=candidate,
        context=context,
    )


def test_foreign_failure_credits_other_paths_and_records_proof(
    case: tuple[CloseCandidate, ConfinementContext],
) -> None:
    candidate, context = case
    credited, audit = _credit(candidate, context)
    assert credited == frozenset(OWNED)
    assert audit[0]["failed_paths"] == [FOREIGN]
    assert audit[0]["owners"] == [
        {"path": FOREIGN, "session_id": "owner", "task_id": "foreign-task"}
    ]
    assert audit[0]["handoff_ids"] == ["handoff"]
    assert audit[0]["rerun_command"] == _run(Path(candidate.root), 4).command
    assert audit[0]["failure_exit_code"] == 1


@pytest.mark.parametrize(
    "defect",
    [
        "unowned",
        "own-session",
        "owned-path",
        "handoff",
        "wrong-owner",
        "early-handoff",
        "wrong-command",
        "wrong-path",
        "no-green",
        "wrong-bytes",
        "unknown-candidate",
        "truncated",
        "unlisted-failure",
        "incomplete-summary",
        "fail-fast",
        "filtered",
    ],
)
def test_invalid_confinement_never_credits(
    case: tuple[CloseCandidate, ConfinementContext],
    defect: str,
) -> None:
    candidate, context = case
    failure = _run(Path(candidate.root), 1, failed=True)
    green = _run(Path(candidate.root), 4)
    if defect == "unowned":
        context = replace(context, owners=())
    elif defect == "own-session":
        context = replace(context, owners=(ForeignValidationOwner(FOREIGN, "developer", "task"),))
    elif defect == "owned-path":
        failure = replace(failure, output=(failure.output or "").replace(FOREIGN, OWNED[0]))
    elif defect == "handoff":
        context = replace(context, handoffs=())
    elif defect in {"wrong-owner", "early-handoff", "wrong-command", "wrong-path"}:
        message = context.handoffs[0]
        if defect == "wrong-owner":
            message = replace(message, to_session="unrelated")
        elif defect == "early-handoff":
            message = replace(message, sent_at=NOW)
        elif defect == "wrong-command":
            message = replace(message, content=FOREIGN)
        else:
            message = replace(message, content=failure.command.replace(FOREIGN, "tests/other.py"))
        context = replace(context, handoffs=(message,))
    elif defect == "no-green":
        green = replace(green, outcome="failure", exit_code=1)
    elif defect == "wrong-bytes":
        (Path(candidate.root) / OWNED[1]).write_text("changed\n")
    elif defect == "unknown-candidate":
        candidate.__dict__["blobs"] = None
    elif defect == "truncated":
        failure = replace(failure, output_truncated=True)
    elif defect == "unlisted-failure":
        failure = replace(failure, output=(failure.output or "").replace("1 failed", "2 failed"))
    elif defect == "incomplete-summary":
        failure = replace(failure, output=f"FAILED {FOREIGN}::test_bad")
    elif defect == "fail-fast":
        failure = replace(failure, command=failure.command + " -x")
    else:
        failure = replace(failure, command=failure.command + " -k chosen")
    assert _credit(candidate, context, failure=failure, green=green) == (frozenset(), [])


def test_close_gate_credits_confined_run_without_relabeling_its_outcome(
    case: tuple[CloseCandidate, ConfinementContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.tasks import close_checklist
    from gobby.tasks.transcript_evidence_models import (
        TranscriptEvidence,
        TranscriptValidationSegment,
    )

    candidate, context = case
    monkeypatch.setattr(close_checklist, "CloseCandidate", lambda *_: candidate)
    root = Path(candidate.root)
    audit = replace(
        _run(root, 5),
        categories=("type_check",),
        matcher_id="gobby-test-types-audit",
        command="uv run gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new",
        validation_segments=(
            TranscriptValidationSegment(
                command="gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new",
                categories=("type_check",),
                languages=("python",),
                bounded_inputs=True,
            ),
        ),
    )
    failure = _run(root, 1, failed=True)
    evidence = TranscriptEvidence(validation_runs=(failure, _run(root, 4), audit))
    result = close_checklist.evaluate_validation_commands(
        task_category="code",
        evidence=evidence,
        has_attributed_edits=True,
        changed_paths=OWNED,
        close_root=candidate.root,
        candidate_commit_sha=candidate.commit,
        confinement_context=context,
    )
    assert result.status == "passed"
    assert result.details["pytest_uncovered_paths"] == []
    assert result.details["confinement_credit"][0]["failure_command"] == failure.command
    assert evidence.validation_runs[0].outcome == "failure"


def test_context_uses_active_foreign_tasks_and_persisted_direct_messages(
    case: tuple[CloseCandidate, ConfinementContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.tasks.transcript_evidence_models import TranscriptEvidence
    from gobby.tasks.validation_confinement import load_confinement_context
    from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES
    from gobby.workflows import state_manager

    candidate, context = case
    db = MagicMock(spec=HubDatabase)
    db.fetchall.side_effect = [
        [{"session_id": "owner", "task_id": "foreign-task"}],
        [context.handoffs[0].to_dict()],
    ]
    variables = MagicMock()
    variables.get_variable_subset.return_value = {
        "task_edited_file_checkouts": {"foreign-task": {"/another/lane": [FOREIGN]}},
    }
    monkeypatch.setattr(state_manager, "SessionVariableManager", lambda _: variables)
    loaded = load_confinement_context(
        db,
        project_id="project",
        closing_session_id="developer",
        evidence=TranscriptEvidence(validation_runs=(_run(Path(candidate.root), 1, failed=True),)),
    )
    assert loaded == context
    sql, params = db.fetchall.call_args_list[0].args
    assert "tasks.closed_at IS NULL" in sql
    assert "sessions.id IS DISTINCT FROM" in sql
    assert params == ("project", "project", "developer", list(TERMINAL_OWNER_STATUSES))
    assert db.fetchall.call_args_list[1].args[1][:2] == (["developer"], ["owner"])


def test_context_without_enumerated_failures_never_queries_database(tmp_path: Path) -> None:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.tasks.transcript_evidence_models import TranscriptEvidence
    from gobby.tasks.validation_confinement import load_confinement_context

    db = MagicMock(spec=HubDatabase)
    assert (
        load_confinement_context(
            db,
            project_id="project",
            closing_session_id="developer",
            evidence=TranscriptEvidence(validation_runs=(_run(tmp_path, 1),)),
        )
        == ConfinementContext()
    )
    db.fetchall.assert_not_called()


def test_foreign_tree_rerun_requires_every_changed_path_to_match(
    case: tuple[CloseCandidate, ConfinementContext],
    tmp_path: Path,
) -> None:
    candidate, context = case
    copy = tmp_path / "copy"
    for path in OWNED:
        destination = copy / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((Path(candidate.root) / path).read_bytes())
    green = _run(copy, 4)
    assert _credit(candidate, context, green=green)[0] == frozenset(OWNED)
    (copy / OWNED[1]).write_text("changed\n")
    assert _credit(candidate, context, green=green) == (frozenset(), [])


@pytest.mark.parametrize("prefix", ["absolute", "uv run python -m pytest"])
@pytest.mark.parametrize("side", ["failure", "rerun"])
def test_python_module_invocation_earns_confinement_credit(
    case: tuple[CloseCandidate, ConfinementContext], prefix: str, side: str
) -> None:
    candidate, context = case
    if prefix == "absolute":
        prefix = str(Path(candidate.root) / ".venv/bin/python") + " -m pytest"
    failure = _run(Path(candidate.root), 1, failed=True)
    green = _run(Path(candidate.root), 4)
    if side == "failure":
        failure = replace(failure, command=failure.command.replace("uv run pytest", prefix))
        context = replace(
            context,
            handoffs=(
                replace(
                    context.handoffs[0],
                    content=f"Failing command: {failure.command}; {FOREIGN}: AssertionError; blocks close.",
                ),
            ),
        )
    else:
        green = replace(green, command=green.command.replace("uv run pytest", prefix))
    paths, audit = _credit(candidate, context, failure=failure, green=green)
    assert paths == frozenset(OWNED)
    assert audit[0]["rerun_command"] == green.command


@pytest.mark.parametrize("option", ["-qk test_ok", "-qm slow", "-vx"])
@pytest.mark.parametrize("side", ["failure", "rerun"])
def test_short_option_clusters_never_earn_confinement_credit(
    case: tuple[CloseCandidate, ConfinementContext], option: str, side: str
) -> None:
    candidate, context = case
    failure = _run(Path(candidate.root), 1, failed=True)
    green = _run(Path(candidate.root), 4)
    if side == "failure":
        failure = replace(failure, command=failure.command + " " + option)
        context = replace(
            context,
            handoffs=(
                replace(
                    context.handoffs[0],
                    content=f"Failing command: {failure.command}; {FOREIGN}: AssertionError; blocks close.",
                ),
            ),
        )
    else:
        green = replace(green, command=green.command + " " + option)
    assert _credit(candidate, context, failure=failure, green=green) == (frozenset(), [])


@pytest.mark.parametrize("option", ["--co", "--setup-plan"])
def test_non_validation_rerun_never_earns_confinement_credit(
    case: tuple[CloseCandidate, ConfinementContext], option: str
) -> None:
    candidate, context = case
    green = _run(Path(candidate.root), 4)
    green = replace(green, command=green.command + " " + option, categories=())
    assert _credit(candidate, context, green=green) == (frozenset(), [])


@pytest.mark.parametrize(
    "option",
    [
        "--sw-skip",
        "--stepwise-skip",
        "-o addopts=--co",
        "-oaddopts=--co",
        "--override-ini addopts=--co",
        "--override-ini=addopts=--co",
        "-o 'addopts = -k chosen'",
    ],
)
@pytest.mark.parametrize("side", ["failure", "rerun"])
def test_hidden_pytest_limits_never_earn_confinement_credit(
    case: tuple[CloseCandidate, ConfinementContext], option: str, side: str
) -> None:
    candidate, context = case
    failure = _run(Path(candidate.root), 1, failed=True)
    green = _run(Path(candidate.root), 4)
    if side == "failure":
        failure = replace(failure, command=failure.command + " " + option)
        context = replace(
            context,
            handoffs=(
                replace(
                    context.handoffs[0],
                    content=f"Failing command: {failure.command}; {FOREIGN}: AssertionError; blocks close.",
                ),
            ),
        )
    else:
        green = replace(green, command=green.command + " " + option)
    assert _credit(candidate, context, failure=failure, green=green) == (frozenset(), [])


@pytest.mark.parametrize("option", ["--co", "--setup-plan"])
@pytest.mark.parametrize("confined", [False, True])
def test_close_gate_does_not_credit_non_validation_pytest_coverage(
    case: tuple[CloseCandidate, ConfinementContext],
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    confined: bool,
) -> None:
    from gobby.tasks import close_checklist
    from gobby.tasks.transcript_evidence_models import (
        TranscriptEvidence,
        TranscriptValidationSegment,
    )

    candidate, context = case
    monkeypatch.setattr(close_checklist, "CloseCandidate", lambda *_: candidate)
    root = Path(candidate.root)
    audit_command = (
        "gobby test-types audit tests/ --baseline .gobby/test-types-baseline.json --fail-on-new"
    )
    audit = replace(
        _run(root, 7),
        categories=("type_check",),
        matcher_id="gobby-test-types-audit",
        command="uv run " + audit_command,
        validation_segments=(
            TranscriptValidationSegment(
                command=audit_command,
                categories=("type_check",),
                languages=("python",),
                bounded_inputs=True,
            ),
        ),
    )
    collection = replace(
        _run(root, 4),
        command="uv run pytest " + " ".join(OWNED) + " " + option,
        categories=(),
    )
    foreign = replace(_run(root, 6), command=f"uv run pytest {FOREIGN} -q")
    runs: tuple[TranscriptValidationRun, ...] = (collection, foreign, audit)
    if confined:
        runs = (_run(root, 1, failed=True), *runs)
    result = close_checklist.evaluate_validation_commands(
        task_category="code",
        evidence=TranscriptEvidence(validation_runs=runs),
        has_attributed_edits=True,
        changed_paths=OWNED,
        close_root=candidate.root,
        candidate_commit_sha=candidate.commit,
        confinement_context=context if confined else None,
    )
    assert result.status == "failed"
    assert result.details["pytest_uncovered_paths"] == list(OWNED)
    assert result.details["confinement_credit"] == []
