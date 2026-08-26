"""Cumulative test guards for leaves within one epic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import subprocess
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.acceptance_artifacts import extract_artifact_references
from gobby.utils.git import run_git_command

logger = logging.getLogger(__name__)

_GUARD_OUTPUT_LIMIT = 32_000
_GUARD_TIMEOUT_SECONDS = 600.0
_REPO_STATE_TIMEOUT_SECONDS = 15
_GUARD_CACHE_LIMIT = 16


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


_passed_guard_runs: OrderedDict[str, EpicGuardResult] = OrderedDict()

#: Closures that leave no repository artifacts behind (see
#: ``gobby.tasks.validation.NO_WORK_CLOSE_REASONS``); their criteria still name
#: acceptance tests nobody wrote. ``already_implemented`` stays a guard source
#: because the tests it names exist.
_ARTIFACT_FREE_CLOSE_REASONS: frozenset[str] = frozenset(
    {"duplicate", "wont_fix", "obsolete", "out_of_repo"}
)


async def evaluate_epic_guards(
    *,
    task_manager: LocalTaskManager,
    task: Task,
    repo_path: str,
    closing_commit_shas: Sequence[str] = (),
    timeout_seconds: float = _GUARD_TIMEOUT_SECONDS,
) -> EpicGuardResult:
    """Collect and run every earlier closed leaf's guard test."""
    # The cached answer is reached before collection, because the key needs the
    # scope's identity rather than its resolved guard paths. Collection costs
    # one `git show` per closed leaf carrying commits -- 53 subprocess spawns on
    # this project, 1.1 s out of process and 7 s inside the daemon -- and a hit
    # needs none of them (#20866). A miss falls through to the original order.
    template, template_error = _load_guard_template(repo_path)
    cache_key = (
        None
        if template_error
        else await asyncio.to_thread(
            _guard_run_cache_key, task_manager, task, template or "", repo_path
        )
    )
    if cache_key is not None:
        cached = _passed_guard_runs.get(cache_key)
        if cached is not None:
            return cached

    # Collection is one scoped query for the epic's subtree since #20847, but it
    # is still synchronous psycopg plus git and filesystem work per guard path,
    # so it stays off the loop. Left on it, the project-wide walk this replaced
    # held the daemon for 66 seconds on a ~15k-task project, with every route
    # including liveness timing out (#20841).
    paths, source_task_ids, collection_errors, sibling_deleted = await asyncio.to_thread(
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
        # A guard test deleted by the closing task's own linked commits -- or
        # by a closed sibling's, whose close gates vetted the deletion -- left
        # with the feature it covered; only unexplained absences block.
        deleted_by_commits: set[str] = set(sibling_deleted)
        for sha in closing_commit_shas:
            try:
                deleted_by_commits.update(await asyncio.to_thread(_deleted_files, sha, repo_path))
            except RuntimeError:
                break
        still_missing = tuple(path for path in missing if path not in deleted_by_commits)
        if still_missing:
            return _result(
                passed=False,
                skipped=False,
                error_type="epic_guard_missing",
                message=f"Epic guard file is missing: {still_missing[0]}",
                paths=paths,
                source_task_ids=source_task_ids,
                template=template,
            )
        paths = tuple(path for path in paths if path not in missing)
        if not paths:
            return _result(
                passed=True,
                skipped=True,
                error_type=None,
                message=(
                    "Every guard test was deleted by linked commits of this "
                    "task or closed siblings."
                ),
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
    passed = EpicGuardResult(
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
    if cache_key is not None:
        _remember_passed_guard_run(cache_key, passed)
    return passed


def _guard_run_cache_key(
    task_manager: LocalTaskManager,
    task: Task,
    template: str,
    repo_path: str,
) -> str | None:
    """Key one guard run by its epic scope and everything git can see.

    The scope is keyed by identity rather than by the guard paths it resolves
    to: the paths are a pure function of these rows and the repository, and
    resolving them costs a `git show` per closed leaf. Every task field that
    can add or drop a guard -- criteria, commits, closure -- moves
    ``updated_at``, so the digest moves with it.

    Returns ``None`` when git cannot describe the tree -- no repository, no
    commit yet, a broken checkout -- because a key that cannot notice a change
    would serve a pass that is no longer true. Without a key the guard runs,
    which is the answer that was always correct.

    The repository path is part of the key. Two checkouts can share a commit,
    a status and a diff while differing in everything git ignores, and the
    guard runs inside one of them.

    Files git ignores are outside the key by design: a guard test and what it
    reads are committed, so a change that matters shows up in one of the three
    commands or in an untracked file's stat.
    """
    scope_digest = _epic_scope_digest(task_manager, task)
    if scope_digest is None:
        return None
    head = run_git_command(
        ["git", "rev-parse", "HEAD"], cwd=repo_path, timeout=_REPO_STATE_TIMEOUT_SECONDS
    )
    status = run_git_command(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo_path,
        timeout=_REPO_STATE_TIMEOUT_SECONDS,
    )
    # HEAD and the status entries name which files moved; only the diff says
    # what the tracked ones now contain, and an edit to an already-dirty
    # tracked file changes nothing else.
    diff = run_git_command(
        ["git", "diff", "HEAD"], cwd=repo_path, timeout=_REPO_STATE_TIMEOUT_SECONDS
    )
    if head is None or status is None or diff is None:
        return None
    parts = [
        repo_path,
        scope_digest,
        template,
        head,
        status,
        diff,
        *_untracked_stats(status, repo_path),
    ]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _epic_scope_digest(task_manager: LocalTaskManager, task: Task) -> str | None:
    """Digest the epic scope's identity, or None when it cannot be read.

    One query, no git. A task that cannot be listed leaves no key, so the guard
    runs -- the behaviour before any of this existed.
    """
    try:
        scope = task_manager.list_epic_guard_scope(task.id)
    except Exception:
        logger.debug("Epic guard scope digest unavailable for task %s", task.id, exc_info=True)
        return None
    # The task under evaluation is in its own epic scope and cannot contribute a
    # guard to itself -- guards come from earlier closed leaves, and this one is
    # open. Its updated_at moves on every blocked close attempt, because that is
    # where the verdict and the failure count are recorded, so keeping it in the
    # digest made each retry invalidate the cache the retry was meant to use.
    rows = sorted(
        (str(row.id), str(row.updated_at), str(row.closed_at))
        for row in scope
        if str(row.id) != str(task.id)
    )
    # The parent is the one self-row field collection still reads: it picks the
    # nearest epic and decides which closed tasks are guard leaves rather than
    # parents of one. Reparenting inside the same epic changes the guard set
    # while leaving the row set and the git state alone (#11037 on 24fa93b992).
    payload = json.dumps([str(task.id), str(task.parent_task_id), rows], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _untracked_stats(status: str, repo_path: str) -> list[str]:
    """Stat every untracked path the status listed.

    An untracked file has no diff, so the status entry is the only trace of it
    and that entry names the path alone. Editing an untracked fixture a guard
    test reads would otherwise leave the key untouched and serve a pass from
    before the edit. Size and mtime cost one lstat each and no reads.
    """
    stats: list[str] = []
    for entry in status.split("\0"):
        if not entry.startswith("?? "):
            continue
        path = entry[3:]
        try:
            info = Path(repo_path, path).lstat()
        except OSError:
            stats.append(f"{path}:gone")
            continue
        stats.append(f"{path}:{info.st_size}:{info.st_mtime_ns}")
    return stats


def _remember_passed_guard_run(cache_key: str, result: EpicGuardResult) -> None:
    """Keep the most recent passing runs, and only the passing ones.

    A failure is the answer that already stopped a close, so re-running it
    costs an attempt that was blocked anyway, and keeping failures out means
    one flaky run cannot block every later attempt on the same state.
    """
    _passed_guard_runs[cache_key] = result
    _passed_guard_runs.move_to_end(cache_key)
    while len(_passed_guard_runs) > _GUARD_CACHE_LIMIT:
        _passed_guard_runs.popitem(last=False)


def collect_epic_guard_paths(
    *,
    task_manager: LocalTaskManager,
    task: Task,
    repo_path: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Collect test refs and added pytest modules from prior closed leaves.

    The fourth element is every path deleted by a closed leaf's linked
    commits: a deletion that passed its own task's close gates retires the
    guard for the whole epic (#20904).
    """
    # The task's ancestors and its nearest epic's subtree, nothing else. Paging
    # the whole project here cost ~105s per close_task preview (#20847).
    tasks = task_manager.list_epic_guard_scope(task.id)
    by_id = {item.id: item for item in tasks}
    epic = _nearest_epic(task, by_id)
    if epic is None:
        return (), (), (), ()

    descendants = _descendant_ids(epic.id, tasks)
    child_parents = {item.parent_task_id for item in tasks if item.parent_task_id}
    leaves = sorted(
        (
            item
            for item in tasks
            if item.id in descendants
            and item.id != task.id
            and item.closed_at is not None
            and item.closed_reason not in _ARTIFACT_FREE_CLOSE_REASONS
            and item.id not in child_parents
        ),
        key=lambda item: (item.closed_at, item.id),
    )
    paths: set[str] = set()
    deleted: set[str] = set()
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
                added, removed = _changed_files(sha, repo_path)
            except RuntimeError as exc:
                errors.append(f"Cannot inspect guard commit {sha}: {exc}")
                continue
            deleted.update(removed)
            for path in added:
                if not is_pytest_module_path(path):
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
    return tuple(sorted(paths)), tuple(source_ids), tuple(errors), tuple(sorted(deleted))


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


def _deleted_files(sha: str, repo_path: str) -> tuple[str, ...]:
    _, deleted = _changed_files(sha, repo_path)
    return deleted


def _changed_files(sha: str, repo_path: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Added and deleted paths for one commit from a single name-status listing.

    Renames are decomposed (--no-renames) so a guard renamed away counts as a
    deletion of its old path and an addition of the new one.
    """
    try:
        result = subprocess.run(
            ["git", "show", "--format=", "--name-status", "--no-renames", sha],
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
    added: list[str] = []
    deleted: list[str] = []
    for line in result.stdout.splitlines():
        status, _, path = line.partition("\t")
        path = path.strip()
        if not path:
            continue
        if status.startswith("A"):
            added.append(path)
        elif status.startswith("D"):
            deleted.append(path)
    return tuple(added), tuple(deleted)


def is_pytest_module_path(path: str) -> bool:
    """A Python module pytest collects when named on the command line.

    The guard template is a pytest command, so only ``test_*.py`` and
    ``*_test.py`` files may be forwarded to it. Directory membership is not
    enough: a ``tests/`` tree also holds fixtures, helpers, ``conftest.py``,
    and other languages' tests, and pytest ends the run with ``ERROR: not
    found`` for any of them (#20957).
    """
    name = PurePosixPath(path).name.casefold()
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_test_convention_path(path: str) -> bool:
    """A test module in any language or any file under a test directory."""
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
    "is_pytest_module_path",
    "is_test_convention_path",
]
