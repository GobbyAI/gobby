"""Strict rung-2 credit for otherwise successful paths in a failed pytest run."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.tasks.close_test_coverage import CloseCandidate, candidate_pytest_paths
from gobby.tasks.command_equivalence import pytest_targets, target_covers
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


@dataclass(frozen=True)
class ForeignValidationOwner:
    path: str
    session_id: str
    task_id: str


@dataclass(frozen=True)
class ConfinementContext:
    owners: tuple[ForeignValidationOwner, ...] = ()
    handoffs: tuple[InterSessionMessage, ...] = ()


def load_confinement_context(
    db: HubDatabase,
    *,
    project_id: str,
    closing_session_id: str,
    evidence: TranscriptEvidence,
) -> ConfinementContext:
    """Project active attribution and durable messages into immutable close inputs."""
    failures = [
        run
        for run in (*evidence.validation_runs, *evidence.command_runs)
        if run.outcome == "failure" and _failure_paths(run)
    ]
    if not failures:
        return ConfinementContext()
    from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.task_claim_state import task_edited_checkout_paths

    rows = db.fetchall(
        """
        SELECT tasks.id AS task_id, sessions.id AS session_id
        FROM tasks JOIN sessions ON sessions.id = tasks.claimed_by_session_id
        WHERE tasks.project_id = %s AND sessions.project_id = %s
          AND tasks.closed_at IS NULL AND sessions.id IS DISTINCT FROM %s
          AND sessions.status = ANY(%s)
        """,
        (project_id, project_id, closing_session_id, list(TERMINAL_OWNER_STATUSES)),
    )
    variables = SessionVariableManager(db)
    owners: set[ForeignValidationOwner] = set()
    for row in rows:
        session_id, task_id = str(row["session_id"]), str(row["task_id"])
        ledger = variables.get_variable_subset(
            session_id,
            (
                "task_edited_file_checkouts",
                "task_edited_file_checkouts_history",
            ),
        )
        for _, path in task_edited_checkout_paths(ledger, task_id):
            owners.add(ForeignValidationOwner(path, session_id, task_id))
    messages = db.fetchall(
        """
        SELECT * FROM inter_session_messages
        WHERE from_session = ANY(%s) AND to_session = ANY(%s) AND sent_at >= %s
        ORDER BY sent_at, id
        """,
        (
            sorted({run.session_id for run in failures}),
            sorted({owner.session_id for owner in owners}),
            min(run.completed_at for run in failures),
        ),
    )
    return ConfinementContext(
        tuple(sorted(owners, key=lambda owner: (owner.path, owner.session_id, owner.task_id))),
        tuple(InterSessionMessage.from_row(row) for row in messages),
    )


_FAILURE_LINE = re.compile(r"^(FAILED|ERROR) ((?:tests/)[^\s]+\.py(?:::[^\s]+)?)(?:\s|$)", re.M)
_SUMMARY = re.compile(r"(?m)^=*[ \t]*((?:\d+ [a-z]+(?:, )?)+) in [\d.]+s(?:.*)$")
_LIMITS = (
    "-k",
    "-m",
    "-x",
    "--exitfirst",
    "--maxfail",
    "--lf",
    "--last-failed",
    "--sw",
    "--stepwise",
    "--sw-skip",
    "--stepwise-skip",
    "--override-ini",
    "--config-file",
    "--collect-only",
    "--deselect",
    "--ignore",
    "--ignore-glob",
)


def _full_pytest(run: TranscriptValidationRun) -> bool:
    if (
        "PYTEST_ADDOPTS" in run.command
        or run.wrapped
        or run.core_command is None
        or pytest_targets(run.core_command) is None
    ):
        return False
    words = shlex.split(run.core_command)
    pytest_index = next(i for i, word in enumerate(words) if word.rsplit("/", 1)[-1] == "pytest")
    arguments = words[pytest_index + 1 :]
    return not any(
        "::" in word
        or word == limit
        or word.startswith(limit + "=")
        or (
            word.startswith("-")
            and not word.startswith("--")
            and set(word[1:]) & {"k", "m", "x", "o", "c"}
        )
        for word in arguments
        for limit in _LIMITS
    )


def _failure_paths(run: TranscriptValidationRun) -> frozenset[str]:
    """Require complete pytest failure enumeration, not arbitrary traceback paths."""
    if run.exit_code != 1 or run.output_truncated or not _full_pytest(run):
        return frozenset()
    output = run.output or ""
    summaries = list(_SUMMARY.finditer(output))
    if not summaries or "Interrupted" in output or "stopping after" in output:
        return frozenset()
    counts = {kind: int(count) for count, kind in re.findall(r"(\d+) ([a-z]+)", summaries[-1][1])}
    failures = {(kind, node) for kind, node in _FAILURE_LINE.findall(output)}
    expected = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    if not counts.get("passed") or not expected or len(failures) != expected:
        return frozenset()
    return frozenset(node.split("::", 1)[0] for _, node in failures)


def _handoffs(
    failure: TranscriptValidationRun,
    rerun: TranscriptValidationRun,
    paths: frozenset[str],
    context: ConfinementContext,
) -> tuple[tuple[ForeignValidationOwner, InterSessionMessage], ...]:
    proof: list[tuple[ForeignValidationOwner, InterSessionMessage]] = []
    for path in sorted(paths):
        match = next(
            (
                (owner, message)
                for owner in context.owners
                if owner.path == path and owner.session_id != failure.session_id
                for message in context.handoffs
                if message.from_session == failure.session_id
                and message.to_session == owner.session_id
                and failure.completed_at <= message.sent_at <= rerun.completed_at
                and (
                    failure.command in message.content
                    or (
                        failure.core_command is not None and failure.core_command in message.content
                    )
                )
                and _names_failure(message.content, failure, path)
            ),
            None,
        )
        if match is None:
            return ()
        proof.append(match)
    return tuple(proof)


def _names_failure(content: str, failure: TranscriptValidationRun, path: str) -> bool:
    # A command in a generic status message is not an owner handoff. Require the
    # failure path and diagnosis outside the quoted command too.
    remainder = content.replace(failure.command, "")
    if failure.core_command:
        remainder = remainder.replace(failure.core_command, "")
    return bool(
        re.search(r"(?<![\w/])" + re.escape(path) + r"(?![\w./])", remainder)
        and re.search(r"\b(?:fail\w*|error\w*|block\w*|assertion\w*)\b", remainder, re.I)
    )


def confinement_credit(
    *,
    runs: Sequence[TranscriptValidationRun],
    required_paths: Sequence[str],
    changed_paths: Sequence[str],
    candidate: CloseCandidate | None,
    context: ConfinementContext | None,
) -> tuple[frozenset[str], list[dict[str, object]]]:
    """Return per-path credit and its audit, never changing a failed run's outcome."""
    if context is None or candidate is None or candidate.blobs is None:
        return frozenset(), []
    credited: set[str] = set()
    audit: list[dict[str, object]] = []
    for failure in runs:
        if failure.outcome != "failure" or failure.categories != ("test",):
            continue
        failed_paths = _failure_paths(failure)
        if not failed_paths or failed_paths.intersection(changed_paths):
            continue
        paths = candidate_pytest_paths(failure, required_paths, changed_paths, candidate)
        paths = paths - failed_paths
        if not paths:
            continue
        for green in runs:
            if (
                green.outcome != "success"
                or green.exit_code != 0
                or green.categories != ("test",)
                or green.session_id != failure.session_id
                or green.started_at < failure.completed_at
                or not _full_pytest(green)
            ):
                continue
            targets = pytest_targets(green.core_command or green.command) or ()
            if any(target_covers(target, path) for target in targets for path in failed_paths):
                continue
            if not candidate_pytest_paths(green, tuple(paths), changed_paths, candidate):
                continue
            handoffs = _handoffs(failure, green, failed_paths, context)
            if not handoffs:
                continue
            credited.update(paths)
            audit.append(
                {
                    "failure_session_id": failure.session_id,
                    "failure_order": failure.order,
                    "failure_command": failure.command,
                    "failure_exit_code": failure.exit_code,
                    "failed_paths": sorted(failed_paths),
                    "owners": [asdict(owner) for owner, _ in handoffs],
                    "handoff_ids": sorted({message.id for _, message in handoffs}),
                    "rerun_command": green.command,
                    "rerun_order": green.order,
                    "rerun_session_id": green.session_id,
                    "candidate_commit_sha": candidate.commit,
                    "credited_paths": sorted(paths),
                }
            )
            break
    return frozenset(credited), audit
