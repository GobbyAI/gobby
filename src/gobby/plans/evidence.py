"""Evidence resolution helpers for plan coverage and review gates."""

from __future__ import annotations

import subprocess  # nosec B404 # resolver shells out to local git.
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

import yaml

from gobby.storage.tasks import MissingIsolationBaseError

__all__ = [
    "EvidenceBundle",
    "EvidenceContextProtocol",
    "EvidenceKind",
    "EvidenceResolveStatus",
    "EvidenceRow",
    "InvalidEvidenceError",
    "MissingIsolationBaseError",
    "resolve_evidence",
]


class EvidenceKind(StrEnum):
    commits = "commits"
    task_diff = "task-diff"
    worktree_diff = "worktree-diff"
    coverage_matrix = "coverage-matrix"
    none = "none"


class EvidenceResolveStatus(StrEnum):
    resolved = "resolved"
    invalid = "invalid"


@dataclass(frozen=True)
class EvidenceRow:
    kind: EvidenceKind
    ref: str
    status: EvidenceResolveStatus
    detail: str
    artifacts_touched: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceBundle:
    rows: tuple[EvidenceRow, ...]
    summary: str


class InvalidEvidenceError(ValueError):
    """Raised when an evidence spec is malformed or unsupported."""


class EvidenceContextProtocol(Protocol):
    repo_root: Path

    def get_task_diff(self, task_ref: str) -> str: ...

    def get_artifacts(self, task_ref: str) -> dict[str, Any] | None: ...

    def get_commit_range_diff(self, range_: str) -> str: ...


def resolve_evidence(spec: str, *, ctx: EvidenceContextProtocol) -> EvidenceBundle:
    """Resolve a single evidence spec into audit rows."""
    if spec == EvidenceKind.none.value:
        return _bundle(
            EvidenceRow(
                kind=EvidenceKind.none,
                ref="none",
                status=EvidenceResolveStatus.resolved,
                detail="explicit operator override",
                artifacts_touched=(),
            )
        )

    kind_value, separator, ref = spec.partition(":")
    if not separator or not ref:
        raise InvalidEvidenceError(f"Invalid evidence spec: {spec}")

    try:
        kind = EvidenceKind(kind_value)
    except ValueError as error:
        raise InvalidEvidenceError(f"Unsupported evidence kind: {kind_value}") from error

    if kind is EvidenceKind.commits:
        return _resolve_commits(ref, ctx=ctx)
    if kind is EvidenceKind.task_diff:
        return _resolve_task_diff(ref, ctx=ctx)
    if kind is EvidenceKind.worktree_diff:
        return _resolve_worktree_diff(ref, ctx=ctx)
    if kind is EvidenceKind.coverage_matrix:
        return _resolve_coverage_matrix(ref, ctx=ctx)

    raise InvalidEvidenceError(f"Unsupported evidence kind: {kind.value}")


def _bundle(*rows: EvidenceRow) -> EvidenceBundle:
    invalid = sum(1 for row in rows if row.status is EvidenceResolveStatus.invalid)
    resolved = len(rows) - invalid
    return EvidenceBundle(
        rows=tuple(rows),
        summary=f"{resolved} resolved, {invalid} invalid",
    )


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 # args are fixed git argv plus caller refs.
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _resolve_commits(range_: str, *, ctx: EvidenceContextProtocol) -> EvidenceBundle:
    rev_list = _run_git(ctx.repo_root, ["rev-list", "--reverse", range_])
    if rev_list.returncode != 0:
        detail = rev_list.stderr.strip() or f"commit range {range_} did not resolve"
        return _bundle(_invalid(EvidenceKind.commits, range_, detail))

    rows: list[EvidenceRow] = []
    for commit_sha in (line.strip() for line in rev_list.stdout.splitlines()):
        if not commit_sha:
            continue
        name_only = _run_git(
            ctx.repo_root,
            ["diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
        )
        if name_only.returncode != 0:
            detail = name_only.stderr.strip() or f"commit {commit_sha} diff did not resolve"
            rows.append(_invalid(EvidenceKind.commits, commit_sha, detail))
            continue
        touched = _dedupe(line.strip() for line in name_only.stdout.splitlines() if line.strip())
        rows.append(
            EvidenceRow(
                kind=EvidenceKind.commits,
                ref=commit_sha,
                status=EvidenceResolveStatus.resolved,
                detail=f"commit {commit_sha}",
                artifacts_touched=touched,
            )
        )

    if not rows:
        diff = ctx.get_commit_range_diff(range_)
        rows.append(
            EvidenceRow(
                kind=EvidenceKind.commits,
                ref=range_,
                status=EvidenceResolveStatus.resolved,
                detail=f"commit range {range_}",
                artifacts_touched=_parse_diff_files(diff),
            )
        )
    return _bundle(*rows)


def _resolve_task_diff(task_ref: str, *, ctx: EvidenceContextProtocol) -> EvidenceBundle:
    diff = ctx.get_task_diff(task_ref)
    return _bundle(
        EvidenceRow(
            kind=EvidenceKind.task_diff,
            ref=task_ref,
            status=EvidenceResolveStatus.resolved,
            detail=f"task diff for {task_ref}",
            artifacts_touched=_parse_diff_files(diff),
        )
    )


def _resolve_worktree_diff(artifact_ref: str, *, ctx: EvidenceContextProtocol) -> EvidenceBundle:
    artifacts = ctx.get_artifacts(artifact_ref)
    if artifacts is None:
        return _bundle(
            _invalid(
                EvidenceKind.worktree_diff, artifact_ref, f"no artifacts row for {artifact_ref}"
            )
        )

    isolation_path = artifacts.get("worktree_path") or artifacts.get("clone_path")
    if not isolation_path:
        return _bundle(
            _invalid(
                EvidenceKind.worktree_diff,
                artifact_ref,
                f"no isolation path on artifacts row for {artifact_ref}",
            )
        )

    base_commit_sha = artifacts.get("base_commit_sha")
    if base_commit_sha is None:
        return _bundle(
            _invalid(
                EvidenceKind.worktree_diff,
                artifact_ref,
                "missing base_commit_sha; rerun gobby build to recapture base "
                "or use set_artifact(base_commit_sha=...) if you can recover it "
                "out-of-band",
            )
        )

    path = Path(cast(str, isolation_path))
    base = cast(str, base_commit_sha)
    rev_parse = _run_git(path, ["rev-parse", base])
    if rev_parse.returncode != 0:
        return _bundle(
            _invalid(
                EvidenceKind.worktree_diff,
                artifact_ref,
                f"base_commit_sha {base} does not resolve in {path}",
            )
        )

    diff = _run_git(path, ["diff", f"{base}...HEAD"])
    if diff.returncode != 0:
        detail = diff.stderr.strip() or f"git diff {base}...HEAD failed in {path}"
        return _bundle(_invalid(EvidenceKind.worktree_diff, artifact_ref, detail))

    touched = _parse_diff_files(diff.stdout)
    return _bundle(
        EvidenceRow(
            kind=EvidenceKind.worktree_diff,
            ref=artifact_ref,
            status=EvidenceResolveStatus.resolved,
            detail=f"diff {base}...HEAD in {path} touched {len(touched)} file(s)",
            artifacts_touched=touched,
        )
    )


def _resolve_coverage_matrix(path_ref: str, *, ctx: EvidenceContextProtocol) -> EvidenceBundle:
    path = Path(path_ref)
    if not path.is_absolute():
        path = ctx.repo_root / path
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidEvidenceError(f"Invalid coverage matrix {path_ref}: {exc}") from exc
    rows_data = _manifest_rows(raw)
    evidence_rows: list[EvidenceRow] = []
    for index, row_data in enumerate(rows_data):
        row = cast(dict[str, Any], row_data)
        ref = _coverage_ref(row, fallback=f"{path_ref}#{index + 1}")
        touched = _coverage_artifacts(row)
        embedded = row.get("evidence")
        if isinstance(embedded, list) and embedded:
            for evidence in embedded:
                if isinstance(evidence, dict):
                    evidence_rows.append(_coverage_embedded_row(evidence, ref, touched))
            continue
        row_status = row.get("status")
        status = (
            EvidenceResolveStatus.invalid
            if row_status in {"invalid", "missing"}
            else EvidenceResolveStatus.resolved
        )
        evidence_rows.append(
            EvidenceRow(
                kind=EvidenceKind.coverage_matrix,
                ref=ref,
                status=status,
                detail=str(row.get("detail") or row_status or "coverage row"),
                artifacts_touched=touched,
            )
        )

    return _bundle(*evidence_rows)


def _manifest_rows(raw: Any) -> list[Any]:
    if isinstance(raw, dict):
        rows = raw.get("rows")
        return rows if isinstance(rows, list) else []
    return raw if isinstance(raw, list) else []


def _coverage_ref(row: dict[str, Any], *, fallback: str) -> str:
    section_id = row.get("section_id")
    item_id = row.get("item_id")
    if section_id is not None and item_id is not None:
        return f"{section_id}:{item_id}"
    return fallback


def _coverage_artifacts(row: dict[str, Any]) -> tuple[str, ...]:
    touched: list[str] = []
    leaves = row.get("leaves")
    if isinstance(leaves, list):
        for leaf in leaves:
            if isinstance(leaf, dict) and leaf.get("matched_artifact_ref") is not None:
                touched.append(str(leaf["matched_artifact_ref"]))
    return _dedupe(touched)


def _coverage_embedded_row(
    evidence: dict[str, Any],
    fallback_ref: str,
    touched: tuple[str, ...],
) -> EvidenceRow:
    try:
        kind = EvidenceKind(str(evidence.get("kind", EvidenceKind.coverage_matrix.value)))
    except ValueError:
        kind = EvidenceKind.coverage_matrix
    try:
        status = EvidenceResolveStatus(
            str(evidence.get("status", EvidenceResolveStatus.resolved.value))
        )
    except ValueError:
        status = EvidenceResolveStatus.invalid
    return EvidenceRow(
        kind=kind,
        ref=str(evidence.get("ref") or fallback_ref),
        status=status,
        detail=str(evidence.get("detail") or evidence.get("reason") or "coverage evidence"),
        artifacts_touched=touched,
    )


def _invalid(kind: EvidenceKind, ref: str, detail: str) -> EvidenceRow:
    return EvidenceRow(
        kind=kind,
        ref=ref,
        status=EvidenceResolveStatus.invalid,
        detail=detail,
        artifacts_touched=(),
    )


def _parse_diff_files(diff: str) -> tuple[str, ...]:
    touched: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        before = parts[2].removeprefix("a/")
        after = parts[3].removeprefix("b/")
        touched.append(after if after != "/dev/null" else before)
    return _dedupe(touched)


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
