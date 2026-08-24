"""Cumulative test guards for leaves within one epic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.acceptance_artifacts import extract_artifact_references

_GUARD_OUTPUT_LIMIT = 32_000
_GUARD_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class EpicGuardResult:
    """Prepared guard set and optional execution outcome."""

    passed: bool
    skipped: bool
    error_type: str | None
    message: str
    paths: tuple[str, ...] = ()
    source_task_ids: tuple[str, ...] = ()
    command: str | None = None
    output: str | None = None
    fingerprint: str = ""

    def details(self) -> dict[str, object]:
        return {
            "paths": list(self.paths),
            "source_task_ids": list(self.source_task_ids),
            "command": self.command,
            "output": self.output,
            "fingerprint": self.fingerprint,
        }

    def review_facts(self) -> dict[str, object]:
        """Guard identity for the criteria review, without the runner's stdout.

        These facts reach the review prompt and, through it, both the review
        and evidence fingerprints. The runner's output carries a fresh duration
        on every run, so including it moved the fingerprint pair on every close
        attempt and made the memoized verdict unreachable (#20866). Nothing is
        lost: a guard that fails stops the close at gate 13, so the text this
        drops is always a success banner, and dropping it also keeps up to
        32,000 characters of unrelated test output out of the prompt.
        """
        facts = self.details()
        del facts["output"]
        return facts


async def evaluate_epic_guards(
    *,
    task_manager: LocalTaskManager,
    task: Task,
    repo_path: str,
    timeout_seconds: float = _GUARD_TIMEOUT_SECONDS,
) -> EpicGuardResult:
    """Collect and run every earlier closed leaf's guard test."""
    # Collection is one scoped query for the epic's subtree since #20847, but it
    # is still synchronous psycopg plus git and filesystem work per guard path,
    # so it stays off the loop. Left on it, the project-wide walk this replaced
    # held the daemon for 66 seconds on a ~15k-task project, with every route
    # including liveness timing out (#20841).
    paths, source_task_ids, collection_errors = await asyncio.to_thread(
        collect_epic_guard_paths,
        task_manager=task_manager,
        task=task,
        repo_path=repo_path,
    )
    if collection_errors:
        return _result(
            passed=False,
            skipped=False,
            error_type="epic_guard_collection_failed",
            message=collection_errors[0],
            paths=paths,
            source_task_ids=source_task_ids,
        )
    if not paths:
        return _result(
            passed=True,
            skipped=True,
            error_type=None,
            message="No earlier closed epic leaves contribute guard tests.",
        )

    template, template_error = _load_guard_template(repo_path)
    if template_error:
        return _result(
            passed=False,
            skipped=False,
            error_type="guard_runner_unconfigured",
            message=template_error,
            paths=paths,
            source_task_ids=source_task_ids,
        )
    missing = tuple(path for path in paths if not Path(repo_path, path).is_file())
    if missing:
        return _result(
            passed=False,
            skipped=False,
            error_type="epic_guard_missing",
            message=f"Epic guard file is missing: {missing[0]}",
            paths=paths,
            source_task_ids=source_task_ids,
            template=template,
        )

    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    command = template.replace("{test_files}", quoted_paths)
    fingerprint = _fingerprint(paths, source_task_ids, template)
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            process.kill()
            stdout, _ = await process.communicate()
            output = _bounded_output(stdout.decode(errors="replace"))
            return EpicGuardResult(
                passed=False,
                skipped=False,
                error_type="epic_guard_timeout",
                message=f"Epic guard command timed out after {timeout_seconds:g} seconds.",
                paths=paths,
                source_task_ids=source_task_ids,
                command=command,
                output=output,
                fingerprint=fingerprint,
            )
    except OSError as exc:
        return EpicGuardResult(
            passed=False,
            skipped=False,
            error_type="epic_guard_execution_failed",
            message=f"Epic guard runner could not start: {exc}",
            paths=paths,
            source_task_ids=source_task_ids,
            command=command,
            fingerprint=fingerprint,
        )

    output = _bounded_output(stdout.decode(errors="replace"))
    if process.returncode != 0:
        return EpicGuardResult(
            passed=False,
            skipped=False,
            error_type="epic_guard_failed",
            message=f"Epic guard tests failed for: {', '.join(paths)}",
            paths=paths,
            source_task_ids=source_task_ids,
            command=command,
            output=output,
            fingerprint=fingerprint,
        )
    return EpicGuardResult(
        passed=True,
        skipped=False,
        error_type=None,
        message="Every cumulative epic guard passed.",
        paths=paths,
        source_task_ids=source_task_ids,
        command=command,
        output=output,
        fingerprint=fingerprint,
    )


def collect_epic_guard_paths(
    *,
    task_manager: LocalTaskManager,
    task: Task,
    repo_path: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Collect test refs and added test-convention files from prior closed leaves."""
    # The task's ancestors and its nearest epic's subtree, nothing else. Paging
    # the whole project here cost ~105s per close_task preview (#20847).
    tasks = task_manager.list_epic_guard_scope(task.id)
    by_id = {item.id: item for item in tasks}
    epic = _nearest_epic(task, by_id)
    if epic is None:
        return (), (), ()

    descendants = _descendant_ids(epic.id, tasks)
    child_parents = {item.parent_task_id for item in tasks if item.parent_task_id}
    leaves = sorted(
        (
            item
            for item in tasks
            if item.id in descendants
            and item.id != task.id
            and item.closed_at is not None
            and item.id not in child_parents
        ),
        key=lambda item: (item.closed_at, item.id),
    )
    paths: set[str] = set()
    source_ids: list[str] = []
    errors: list[str] = []
    for leaf in leaves:
        contributed = False
        for reference in extract_artifact_references(leaf.validation_criteria or "", "test"):
            path = reference.split("::", 1)[0]
            error = _path_error(path, repo_path)
            if error:
                errors.append(f"Guard path from task #{leaf.seq_num or leaf.id}: {path}: {error}")
                continue
            paths.add(path)
            contributed = True
        for sha in leaf.commits or ([leaf.closed_commit_sha] if leaf.closed_commit_sha else []):
            try:
                added = _added_files(sha, repo_path)
            except RuntimeError as exc:
                errors.append(f"Cannot inspect guard commit {sha}: {exc}")
                continue
            for path in added:
                if not is_test_convention_path(path):
                    continue
                error = _path_error(path, repo_path)
                if error:
                    errors.append(
                        f"Guard path from task #{leaf.seq_num or leaf.id}: {path}: {error}"
                    )
                    continue
                paths.add(path)
                contributed = True
        if contributed:
            source_ids.append(leaf.id)
    return tuple(sorted(paths)), tuple(source_ids), tuple(errors)


def _nearest_epic(task: Task, by_id: dict[str, Task]) -> Task | None:
    parent_id = task.parent_task_id
    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            return None
        if parent.task_type == "epic":
            return parent
        parent_id = parent.parent_task_id
    return None


def _descendant_ids(parent_id: str, tasks: list[Task]) -> set[str]:
    by_parent: dict[str, list[str]] = {}
    for item in tasks:
        if item.parent_task_id:
            by_parent.setdefault(item.parent_task_id, []).append(item.id)
    descendants: set[str] = set()
    pending = list(by_parent.get(parent_id, ()))
    while pending:
        item_id = pending.pop()
        if item_id in descendants:
            continue
        descendants.add(item_id)
        pending.extend(by_parent.get(item_id, ()))
    return descendants


def _added_files(sha: str, repo_path: str) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "show", "--format=", "--name-only", "--diff-filter=A", sha],
            cwd=repo_path,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git show exited {result.returncode}")
    return tuple(path.strip() for path in result.stdout.splitlines() if path.strip())


def is_test_convention_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    return (
        any(part.casefold() in {"test", "tests", "__tests__"} for part in pure.parts[:-1])
        or name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    )


def _load_guard_template(repo_path: str) -> tuple[str, str | None]:
    path = Path(repo_path, ".gobby", "project.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        template = payload["verification"]["custom"]["guard_tests"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return "", (
            "Configure verification.custom.guard_tests in .gobby/project.json "
            "with exactly one {test_files} placeholder."
        )
    if not isinstance(template, str) or template.count("{test_files}") != 1:
        return "", (
            "verification.custom.guard_tests must be a string containing exactly "
            "one {test_files} placeholder."
        )
    return template, None


def _path_error(path: str, repo_path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        return "path traversal is forbidden"
    root = Path(repo_path).resolve()
    candidate = (root / Path(*pure.parts)).resolve(strict=False)
    if not candidate.is_relative_to(root):
        return "path resolves outside the repository"
    return None


def _fingerprint(
    paths: tuple[str, ...],
    source_task_ids: tuple[str, ...],
    template: str,
) -> str:
    payload = json.dumps(
        {"paths": paths, "source_task_ids": source_task_ids, "template": template},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _bounded_output(output: str) -> str:
    if len(output) <= _GUARD_OUTPUT_LIMIT:
        return output
    marker = "\n...[guard output truncated]...\n"
    half = (_GUARD_OUTPUT_LIMIT - len(marker)) // 2
    return f"{output[:half]}{marker}{output[-half:]}"


def _result(
    *,
    passed: bool,
    skipped: bool,
    error_type: str | None,
    message: str,
    paths: tuple[str, ...] = (),
    source_task_ids: tuple[str, ...] = (),
    template: str = "",
) -> EpicGuardResult:
    return EpicGuardResult(
        passed=passed,
        skipped=skipped,
        error_type=error_type,
        message=message,
        paths=paths,
        source_task_ids=source_task_ids,
        fingerprint=_fingerprint(paths, source_task_ids, template),
    )


__all__ = [
    "EpicGuardResult",
    "collect_epic_guard_paths",
    "evaluate_epic_guards",
    "is_test_convention_path",
]
