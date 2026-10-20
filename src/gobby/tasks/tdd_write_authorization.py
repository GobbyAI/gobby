"""Authorize production writes only after every named behavior test has its own RED."""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from gobby.storage.session_models import Session
from gobby.tasks.acceptance_artifacts import (
    AcceptanceTest,
    _extract_braced_test_body,
    extract_artifact_references,
    parse_test_reference,
)
from gobby.tasks.tdd_evidence import _find_red_run, _green_run_covers_test
from gobby.tasks.tdd_paths import is_implementation_edit_path
from gobby.tasks.tdd_python_evidence import _test_node_from_module
from gobby.tasks.transcript_evidence_models import TranscriptEdit, TranscriptEvidence


@dataclass
class TddObligation:
    """One named test a TDD task's behavior or regression criterion requires."""

    task_id: str
    criterion: str
    test: AcceptanceTest
    regression_only: bool = False


@dataclass
class WriteAuthorization:
    """Whether a production write may proceed, with missing and credited REDs."""

    allowed: bool
    missing: tuple[str, ...]
    red_runs: Mapping[str, str]


def task_tdd_obligations(
    task_id: str, validation_criteria: str | None
) -> tuple[TddObligation, ...]:
    obligations: list[TddObligation] = []
    for criterion in re.split(r"\n(?=\s*\d+(?:\.\d+)*[.)]?\s)", validation_criteria or ""):
        regression = bool(
            re.match(r"\s*(?:\d+(?:\.\d+)*[.)]?\s+)?regression-only:", criterion, re.I)
        )
        for reference in extract_artifact_references(criterion, "test"):
            parsed = parse_test_reference(reference)
            path, symbol = parsed or (reference, "")
            obligations.append(
                TddObligation(
                    task_id, criterion, AcceptanceTest(reference, path, symbol, ""), regression
                )
            )
    behavior = {item.test.reference for item in obligations if not item.regression_only}
    return tuple(
        replace(item, regression_only=False) if item.test.reference in behavior else item
        for item in obligations
    )


def authorize_production_write(
    obligations: tuple[TddObligation, ...],
    evidence: TranscriptEvidence,
    *,
    task_refs: frozenset[str],
    pending_writes: tuple[TranscriptEdit, ...] = (),
) -> WriteAuthorization:
    claims = sorted(evidence.task_claims, key=lambda claim: claim.claimed_at)

    def owned(at: datetime) -> bool:
        preceding = [claim for claim in claims if claim.claimed_at <= at]
        return bool(preceding and preceding[-1].task_ref in task_refs)

    scoped = replace(
        evidence,
        edits=tuple(
            edit for edit in evidence.edits if edit.task_attributed and owned(edit.timestamp)
        ),
        validation_runs=tuple(
            run
            for run in evidence.validation_runs
            if run.completed_at is not None
            and owned(run.started_at)
            and owned(run.completed_at)
            and not any(
                claim.task_ref not in task_refs
                and run.started_at < claim.claimed_at <= (run.completed_at or run.started_at)
                for claim in claims
            )
        ),
    )
    events = [edit.timestamp for edit in evidence.edits]
    events.extend(run.completed_at or run.started_at for run in evidence.validation_runs)
    boundary = TranscriptEdit(
        session_id="write-boundary",
        source="server",
        path="",
        tool_name="Write",
        timestamp=max(events, default=datetime.now(UTC)) + timedelta(microseconds=1),
        order=max(
            [edit.order for edit in evidence.edits]
            + [run.order for run in evidence.validation_runs],
            default=-1,
        )
        + 1,
    )
    missing: list[str] = []
    reds: dict[str, str] = {}
    for obligation in obligations:
        test = obligation.test
        edits = [edit for edit in evidence.edits if edit.path == test.path]
        if obligation.task_id not in task_refs or not test.symbol:
            missing.append(test.reference)
            continue
        if obligation.regression_only and not edits:
            continue
        latest_edit = max((edit.order for edit in edits), default=-1)
        red = None
        for run in sorted(scoped.validation_runs, key=lambda run: run.order):
            prior_production = any(
                edit.order < run.order
                and is_implementation_edit_path(
                    edit.path,
                    named_test_paths=frozenset(item.test.path for item in obligations),
                    test_infrastructure_paths=frozenset(),
                )
                for edit in scoped.edits
            )
            prior_green = any(
                earlier.outcome == "success"
                and earlier.order < latest_edit
                and _green_run_covers_test(earlier, test)
                for earlier in scoped.validation_runs
            )
            for closing in pending_writes or (boundary,):
                red, _ = _find_red_run(
                    test,
                    replace(scoped, validation_runs=(run,)),
                    latest_edit,
                    closing,
                    require_not_implemented=not test.path.endswith(".rs")
                    and (prior_production or prior_green),
                )
                if red is not None:
                    break
            if red is not None:
                break
        if red is None:
            missing.append(test.reference)
        else:
            reds[test.reference] = red.command
    return WriteAuthorization(
        bool(obligations) and not missing, tuple(dict.fromkeys(missing)), reds
    )


def authorize_session_write(
    session: Session,
    obligations: tuple[TddObligation, ...],
    *,
    task_refs: frozenset[str],
    repo_path: str,
    archive_dir: str | None = None,
    task_edited_files: set[str] | None = None,
    pending_sources: Mapping[str, str] | None = None,
) -> WriteAuthorization:
    from gobby.config.validation_detection import default_validation_detection_config
    from gobby.tasks.command_equivalence import run_location
    from gobby.tasks.transcript_evidence import _derive_transcript_evidence_sync

    known = (
        (task_edited_files or set())
        | {obligation.test.path for obligation in obligations}
        | set(pending_sources or ())
    )
    evidence, _ = _derive_transcript_evidence_sync(
        session,
        None,
        default_validation_detection_config(),
        known,
        repo_path,
        frozenset((str(Path(repo_path).resolve()), path) for path in known),
        archive_dir,
        session.machine_id,
        None,
    )
    root = Path(repo_path).resolve()
    evidence = replace(
        evidence,
        validation_runs=tuple(
            run
            for run in evidence.validation_runs
            if (location := run_location(run.command, workdir=run.workdir)) is not None
            and Path(location).is_absolute()
            and Path(location).resolve() == root
        ),
    )
    resolved: list[TddObligation] = []
    sources: dict[str, str] = {}
    missing: list[str] = []
    for obligation in obligations:
        test = obligation.test
        path = (root / test.path).resolve()
        if not path.is_relative_to(root):
            missing.append(test.reference)
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            missing.append(test.reference)
            continue
        sources[test.path] = source
        try:
            if test.path.endswith(".rs"):
                test = _current_rust_test(test, root, source)
            elif not test.path.endswith(".py"):
                test = replace(test, body=_extract_braced_test_body(source, test.symbol, test.path))
        except (OSError, RuntimeError, ValueError):
            missing.append(test.reference)
            continue
        resolved.append(replace(obligation, test=test))
    pending_order = (
        max(
            [edit.order for edit in evidence.edits]
            + [run.order for run in evidence.validation_runs],
            default=-1,
        )
        + 1
    )
    pending_writes = tuple(
        TranscriptEdit(
            session_id=session.id,
            source=session.source,
            path=path,
            tool_name="pending-write",
            timestamp=datetime.now(UTC),
            order=pending_order,
            checkout_root=str(root),
            source_after=source,
        )
        for path, source in (pending_sources or {}).items()
        if (root / path).resolve().is_relative_to(root)
    )
    authorization = authorize_production_write(
        tuple(resolved), evidence, task_refs=task_refs, pending_writes=pending_writes
    )
    missing.extend(authorization.missing)
    for obligation in resolved:
        test = obligation.test
        source = sources[test.path]
        if pending_sources is not None and pending_sources.get(test.path, source) != source:
            missing.append(test.reference)
        latest = max(
            (edit for edit in evidence.edits if edit.path == test.path),
            key=lambda edit: edit.order,
            default=None,
        )
        if latest is not None and (
            latest.source_after is None
            or not latest.source_confirmed
            or latest.source_after != source
        ):
            missing.append(test.reference)
        if latest is None and not obligation.regression_only:
            missing.append(test.reference)
        if test.path.endswith(".py"):
            try:
                module = ast.parse(source)
            except SyntaxError:
                missing.append(test.reference)
                continue
            if (
                _test_node_from_module(replace(test, symbol=test.symbol.split("[", 1)[0]), module)
                is None
            ):
                missing.append(test.reference)
        if obligation.regression_only and latest is None:
            claims = sorted(evidence.task_claims, key=lambda claim: claim.claimed_at)
            own = [claim for claim in claims if claim.task_ref in task_refs]
            if not own or not _unchanged_at_claim(root, test.path, source, own[0].claimed_at):
                missing.append(test.reference)
    missing_refs = tuple(dict.fromkeys(missing))
    return WriteAuthorization(
        authorization.allowed and not missing_refs,
        missing_refs,
        {
            ref: command
            for ref, command in authorization.red_runs.items()
            if ref not in missing_refs
        },
    )


def _current_rust_test(test: AcceptanceTest, root: Path, source: str) -> AcceptanceTest:
    from gobby.tasks.acceptance_rust_modules import (
        RustModuleDeclarations,
        _collect_declarations,
        rust_inline_test_module,
        rust_module_from_declarations,
        rust_target_from_manifest,
    )

    parts = PurePosixPath(test.path).parts
    crate = PurePosixPath(*parts[:2]) if parts[:1] == ("crates",) else PurePosixPath(".")
    declarations: RustModuleDeclarations = {}
    for directory in ("src", "tests"):
        for file in (root / crate / directory).rglob("*.rs"):
            if file.resolve().is_relative_to(root):
                _collect_declarations(
                    declarations,
                    file.relative_to(root).as_posix(),
                    file.read_text(encoding="utf-8"),
                )
    body = _extract_braced_test_body(source, test.symbol, test.path)
    module = rust_module_from_declarations(test.path, declarations)
    target = rust_target_from_manifest(
        test.path, (root / crate / "Cargo.toml").read_text(encoding="utf-8")
    )
    return replace(
        test,
        body=body,
        rust_module=(*module, *rust_inline_test_module(source, test.symbol)),
        rust_target=target,
    )


def _unchanged_at_claim(root: Path, path: str, source: str, at: datetime) -> bool:
    """Prove an explicit regression still matches the committed claim baseline.

    Without reachable baseline bytes the exemption stays closed. Original edit
    receipts independently revoke it even if a later edit restores those bytes.
    """
    from gobby.tasks.transcript_git_sources import _blob

    try:
        result = subprocess.run(
            ["git", "rev-list", "-1", f"--before={at.isoformat()}", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    baseline = result.stdout.strip()
    return result.returncode == 0 and bool(baseline) and _blob(str(root), baseline, path) == source
