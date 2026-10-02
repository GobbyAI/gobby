"""Tests for task mandate path comparison."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._task_scope as task_scope
from gobby.mcp_proxy.tools.tasks._task_scope import (
    TaskScopeEvaluation,
    collect_commit_paths,
    collect_declared_task_targets,
    evaluate_task_scope,
    find_targets_not_found,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def test_explicit_target_with_unknown_suffix_keeps_unrelated_paths_out_of_scope() -> None:
    result = _evaluate(
        description="Targets:\n- `ops/cron.example`\n\nAcceptance:\n- schedule deployed",
        annotations=[_annotation("src/schedule.py", "expansion")],
        actual_paths={"ops/cron.example", "src/schedule.py", "ops/unrelated.example"},
    )
    assert result.declared_paths == ("ops/cron.example", "src/schedule.py")
    assert result.out_of_scope_paths == ("ops/unrelated.example",)
    assert result.accepted is False


@pytest.mark.parametrize("target", ["ops/cron.example", "ops/Caddyfile", "Dockerfile", ".env"])
def test_explicit_targets_are_not_limited_to_known_suffixes(target: str) -> None:
    targets = collect_declared_task_targets(
        f"Targets: `{target}` — config for `src/context.py`\n"
        "Consumers unchanged: `src/untouched.py`\n"
        "**Acceptance:**\n- Verify `tests/test_schedule.py`"
    )
    assert targets == {target}


def test_inline_targets_preserve_extensionless_files_and_reject_external_paths() -> None:
    targets = collect_declared_task_targets(
        "Targets: Dockerfile, ops/cron.example, `src/app.py`\n"
        "Target: `../outside.py`\nTarget: `/absolute.py`\nTarget: `https://host/file.py`"
    )
    assert targets == {"Dockerfile", "ops/cron.example", "src/app.py"}


def _task(description: str = "", validation_criteria: str | None = None) -> Task:
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    return Task(
        id="00000000-0000-4000-8000-000000000101",
        project_id="00000000-0000-4000-8000-000000000201",
        title="Bound task scope",
        category="code",
        priority=2,
        task_type="task",
        created_at=now,
        updated_at=now,
        description=description,
        validation_criteria=validation_criteria,
    )


def _annotation(path: str, source: str) -> SimpleNamespace:
    return SimpleNamespace(file_path=path, annotation_source=source)


def _evaluate(
    *,
    description: str = "",
    validation_criteria: str | None = None,
    annotations: list[SimpleNamespace],
    actual_paths: set[str],
    justification: str | None = None,
    repo_path: str | None = None,
) -> TaskScopeEvaluation:
    with patch.object(TaskAffectedFileManager, "get_files", return_value=annotations):
        return evaluate_task_scope(
            db=MagicMock(),
            task=_task(description, validation_criteria),
            commit_shas=(),
            attributed_paths=actual_paths,
            repo_path=repo_path,
            scope_justification=justification,
        )


def test_declared_targets_combine_description_and_affected_files(tmp_path: Path) -> None:
    existing = tmp_path / "src/gobby/tasks/existing.py"
    existing.parent.mkdir(parents=True)
    existing.touch()

    targets = collect_declared_task_targets(
        description="Targets:\n- src/gobby/tasks/existing.py::future_symbol",
        affected_files=[
            "src/gobby/tasks/missing.py",
            "./src/gobby/tasks/missing.py",
            "../outside.py",
        ],
    )

    assert targets == {"src/gobby/tasks/existing.py", "src/gobby/tasks/missing.py"}
    assert find_targets_not_found(str(tmp_path), targets) == ["src/gobby/tasks/missing.py"]


def test_tests_mirror_of_declared_source_stays_in_scope() -> None:
    evaluation = _evaluate(
        annotations=[_annotation("src/gobby/terminals/native_runtime.py", "manual")],
        actual_paths={
            "src/gobby/terminals/native_runtime.py",
            "tests/cli/test_cli_daemon.py",
        },
    )

    assert evaluation.accepted is True
    assert evaluation.out_of_scope_paths == ()


def test_directory_entry_without_trailing_slash_covers_children(tmp_path: Path) -> None:
    (tmp_path / "src/gobby/tasks").mkdir(parents=True)

    evaluation = _evaluate(
        annotations=[_annotation("src/gobby/tasks", "manual")],
        actual_paths={"src/gobby/tasks/_task_scope.py"},
        repo_path=str(tmp_path),
    )

    assert evaluation.declared_paths == ("src/gobby/tasks",)
    assert evaluation.out_of_scope_paths == ()


def test_criteria_test_references_expand_declared_scope() -> None:
    evaluation = _evaluate(
        description="Targets:\n- src/gobby/tasks/_task_scope.py",
        validation_criteria=(
            "- test: `tests/mcp_proxy/tools/tasks/test_task_scope.py::test_criteria`\n"
            "- file: `docs/evidence/task-scope.md`"
        ),
        annotations=[],
        actual_paths={
            "tests/mcp_proxy/tools/tasks/test_task_scope.py",
            "docs/evidence/task-scope.md",
        },
    )

    assert evaluation.declared_paths == (
        "docs/evidence/task-scope.md",
        "src/gobby/tasks/_task_scope.py",
        "tests/mcp_proxy/tools/tasks/test_task_scope.py",
    )
    assert evaluation.out_of_scope_paths == ()


def test_criteria_refs_do_not_create_scope_without_targets() -> None:
    evaluation = _evaluate(
        validation_criteria=(
            "- test: `tests/mcp_proxy/tools/tasks/test_task_scope.py::test_criteria`\n"
            "- file: `docs/evidence/task-scope.md`"
        ),
        annotations=[],
        actual_paths={
            "src/gobby/mcp_proxy/tools/tasks/_task_scope.py",
            "tests/mcp_proxy/tools/tasks/test_task_scope.py",
        },
    )

    assert evaluation.declared_paths == ()
    assert evaluation.out_of_scope_paths == ()


def test_inline_reference_examples_do_not_expand_declared_scope() -> None:
    evaluation = _evaluate(
        validation_criteria=(
            "For example, `test: tests/other/test_feature.py::test_feature` and "
            "file: docs/evidence/example.md are reference syntax examples."
        ),
        annotations=[_annotation("src/gobby/tasks/acceptance_artifacts.py", "manual")],
        actual_paths={
            "docs/evidence/example.md",
            "src/gobby/tasks/acceptance_artifacts.py",
            "tests/other/test_feature.py",
        },
    )

    assert evaluation.declared_paths == ("src/gobby/tasks/acceptance_artifacts.py",)
    assert evaluation.out_of_scope_paths == ("docs/evidence/example.md",)


def test_production_refactor_exceeds_test_only_scope() -> None:
    evaluation = _evaluate(
        annotations=[_annotation("tests/", "manual")],
        actual_paths={"tests/test_service.py", "src/gobby/service.py"},
    )

    assert evaluation.accepted is False
    assert evaluation.out_of_scope_paths == ("src/gobby/service.py",)
    assert evaluation.justification_error == (
        "A scope_justification is required for out-of-scope paths."
    )


def test_unrelated_lint_edit_requires_bounded_justification() -> None:
    description = """Implementation plan.

Targets:
- `src/gobby/workflows/commit_guard.py::*` — enforce the commit boundary.

Acceptance:
- Focused checks pass.
"""
    too_short = _evaluate(
        description=description,
        annotations=[],
        actual_paths={"src/gobby/workflows/commit_guard.py", "src/gobby/cli/main.py"},
        justification="fixed lint",
    )

    assert too_short.accepted is False
    assert too_short.out_of_scope_paths == ("src/gobby/cli/main.py",)
    assert too_short.justification_error == "scope_justification must be at least 20 characters."

    justification = "The shared lint helper is required by this commit guard change."
    accepted = _evaluate(
        description=description,
        annotations=[],
        actual_paths={"src/gobby/workflows/commit_guard.py", "src/gobby/cli/main.py"},
        justification=justification,
    )
    assert accepted.accepted is True
    assert accepted.scope_justification == justification


def test_observed_annotations_do_not_expand_declared_scope() -> None:
    evaluation = _evaluate(
        annotations=[
            _annotation("tests/", "manual"),
            _annotation("src/gobby/service.py", "observed"),
        ],
        actual_paths={"src/gobby/service.py"},
    )

    assert evaluation.declared_paths == ("tests/",)
    assert evaluation.out_of_scope_paths == ("src/gobby/service.py",)


def test_hypothesis_scope_reports_advisory_drift_without_blocking() -> None:
    evaluation = _evaluate(
        annotations=[_annotation("src/gobby/expected.py", "hypothesis")],
        actual_paths={"src/gobby/expected.py", "src/gobby/service.py"},
    )

    assert evaluation.accepted is True
    assert evaluation.declared_paths == ()
    assert evaluation.out_of_scope_paths == ()
    assert evaluation.advisory_paths == ("src/gobby/expected.py",)
    assert evaluation.advisory_scope_drift == ("src/gobby/service.py",)
    assert evaluation.justification_error is None
    assert evaluation.details()["advisory_scope_drift"] == ["src/gobby/service.py"]


def test_hypothesis_scope_inspects_linked_commits() -> None:
    annotations = [_annotation("src/gobby/expected.py", "hypothesis")]
    with (
        patch.object(TaskAffectedFileManager, "get_files", return_value=annotations),
        patch(
            "gobby.mcp_proxy.tools.tasks._task_scope.collect_commit_paths_async",
            new=AsyncMock(return_value={"src/gobby/service.py"}),
        ) as collect_paths,
    ):
        evaluation = evaluate_task_scope(
            db=MagicMock(),
            task=_task(),
            commit_shas=("abc123",),
            attributed_paths=(),
            repo_path="/repo",
            scope_justification=None,
        )

    assert evaluation.accepted is True
    assert evaluation.advisory_scope_drift == ("src/gobby/service.py",)
    collect_paths.assert_called_once_with(["abc123"], "/repo")


def test_rescope_immediately_replaces_close_and_review_scope(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=str(sample_project["id"]),
        title="Rescope task",
        task_type="task",
        validation_criteria="Rescoping changes the close and review scope gate.",
    )
    files = TaskAffectedFileManager(temp_db)
    files.set_files(task.id, ["src/old.py"], source="expansion")

    manager.update_task(task.id, affected_files=["src/new.py"])
    accepted = evaluate_task_scope(
        db=temp_db,
        task=task,
        commit_shas=(),
        attributed_paths={"src/new.py"},
        repo_path=None,
        scope_justification=None,
    )
    stale = evaluate_task_scope(
        db=temp_db,
        task=task,
        commit_shas=(),
        attributed_paths={"src/old.py"},
        repo_path=None,
        scope_justification=None,
    )

    assert accepted.accepted is True
    assert accepted.declared_paths == ("src/new.py",)
    assert stale.accepted is False
    assert stale.out_of_scope_paths == ("src/old.py",)


def test_no_declared_scope_skips_linked_commit_inspection() -> None:
    with patch.object(TaskAffectedFileManager, "get_files", return_value=[]):
        evaluation = evaluate_task_scope(
            db=MagicMock(),
            task=_task(),
            commit_shas=("missing-commit",),
            attributed_paths={"src/gobby/service.py"},
            repo_path=None,
            scope_justification=None,
        )

    assert evaluation.accepted is True
    assert evaluation.declared_paths == ()
    assert evaluation.actual_paths == ("src/gobby/service.py",)


def test_collect_commit_paths_includes_root_and_later_commits(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    first = tmp_path / "tests" / "test_service.py"
    first.parent.mkdir()
    first.write_text("def test_service():\n    assert True\n")
    newline_path = tmp_path / "tests" / "test_line\nfeed.py"
    newline_path.write_text("def test_line_feed():\n    assert True\n")
    git("add", "tests/test_service.py", "tests/test_line\nfeed.py")
    git("commit", "-qm", "root")
    root_sha = git("rev-parse", "HEAD")

    second = tmp_path / "src" / "gobby" / "service.py"
    second.parent.mkdir(parents=True)
    second.write_text("VALUE = 1\n")
    backslash_path = tmp_path / "src" / "gobby" / "back\\slash.py"
    backslash_path.write_text("VALUE = 2\n")
    git("add", "src/gobby/service.py", "src/gobby/back\\slash.py")
    git("commit", "-qm", "later")
    later_sha = git("rev-parse", "HEAD")

    assert collect_commit_paths((root_sha, later_sha), str(tmp_path)) == {
        "src/gobby/back\\slash.py",
        "src/gobby/service.py",
        "tests/test_line\nfeed.py",
        "tests/test_service.py",
    }


def _git_repo(tmp_path: Path) -> Callable[..., str]:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    return git


async def test_net_commit_paths_drop_a_file_a_later_link_reverts(tmp_path: Path) -> None:
    git = _git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    git("add", ".")
    git("commit", "-qm", "base")
    (tmp_path / "src" / "a.py").write_text("VALUE = 2\n")
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert 1\n")
    git("commit", "-qam", "change both")
    change_sha = git("rev-parse", "HEAD")
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    git("commit", "-qam", "revert the test")
    revert_sha = git("rev-parse", "HEAD")

    net = await task_scope.collect_net_commit_paths_async([change_sha, revert_sha], str(tmp_path))

    assert net == task_scope.NetCommitPaths(changed=frozenset({"src/a.py"}), deleted=frozenset())


def _synced_side_branch(tmp_path: Path, *, conflict: bool, land: bool) -> list[str]:
    """Build a side branch that syncs main in, fixes, and optionally lands; return its links."""
    git = _git_repo(tmp_path)
    git("checkout", "-qb", "main")
    (tmp_path / "shared.py").write_text("BASE = True\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "side")
    (tmp_path / "feature.py").write_text("FEATURE = 1\n")
    if conflict:
        (tmp_path / "shared.py").write_text("SIDE = True\n")
    git("add", ".")
    git("commit", "-qm", "side feature")
    linked = [git("rev-parse", "HEAD")]
    git("checkout", "-q", "main")
    (tmp_path / "unrelated.py").write_text("UNRELATED = True\n")
    if conflict:
        (tmp_path / "shared.py").write_text("MAIN = True\n")
    git("add", ".")
    git("commit", "-qm", "main work")
    git("checkout", "-q", "side")
    subprocess.run(
        ["git", "merge", "--no-ff", "-q", "-m", "sync main", "main"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
    )
    if conflict:
        (tmp_path / "shared.py").write_text("RESOLVED = True\n")
        git("add", "shared.py")
        git("commit", "-qm", "sync main")
    linked.append(git("rev-parse", "HEAD"))
    (tmp_path / "feature.py").write_text("FEATURE = 2\n")
    git("commit", "-qam", "side fix")
    linked.append(git("rev-parse", "HEAD"))
    if land:
        git("checkout", "-q", "main")
        git("merge", "--no-ff", "-q", "-m", "land side", "side")
        linked.append(git("rev-parse", "HEAD"))
    return linked


@pytest.mark.parametrize(
    ("conflict", "land", "expected"),
    [
        (False, False, {"feature.py"}),
        (True, False, {"feature.py", "shared.py"}),
        (False, True, {"feature.py"}),
        (True, True, {"feature.py", "shared.py"}),
    ],
    ids=["clean-sync", "conflict-sync", "clean-landing", "conflict-landing"],
)
async def test_net_commit_paths_exclude_what_a_sync_merge_brought_in(
    tmp_path: Path, conflict: bool, land: bool, expected: set[str]
) -> None:
    linked = _synced_side_branch(tmp_path, conflict=conflict, land=land)

    net = await task_scope.collect_net_commit_paths_async(linked, str(tmp_path))

    assert net == task_scope.NetCommitPaths(changed=frozenset(expected))


async def test_net_commit_paths_report_deletions_absent_from_the_candidate(
    tmp_path: Path,
) -> None:
    git = _git_repo(tmp_path)
    names = ("test_gone.py", "tests/test_moved.py", "tests/test_readded.py", "tests/test_kept.py")
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("def test_it():\n    assert True\n")
    git("add", *names)
    git("commit", "-qm", "root")

    git("rm", "-q", "test_gone.py", "tests/test_readded.py")
    git("mv", "tests/test_moved.py", "tests/test_renamed.py")
    (tmp_path / "tests" / "test_kept.py").write_text("def test_it():\n    assert 1\n")
    git("commit", "-qam", "delete, rename and modify")
    delete_sha = git("rev-parse", "HEAD")

    (tmp_path / "tests" / "test_readded.py").write_text("def test_it():\n    assert True\n")
    git("add", "tests/test_readded.py")
    git("commit", "-qm", "readd")
    readd_sha = git("rev-parse", "HEAD")
    # Tracked at HEAD but missing from the worktree is not a deletion.
    (tmp_path / "tests" / "test_readded.py").unlink()

    net = await task_scope.collect_net_commit_paths_async([delete_sha, readd_sha], str(tmp_path))

    # The deleted-then-readded test nets to nothing; a rename deletes its source.
    assert net == task_scope.NetCommitPaths(
        changed=frozenset(
            {"test_gone.py", "tests/test_moved.py", "tests/test_renamed.py", "tests/test_kept.py"}
        ),
        deleted=frozenset({"test_gone.py", "tests/test_moved.py"}),
    )


async def test_net_commit_paths_fail_closed_on_an_unknown_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    with pytest.raises(RuntimeError, match="Cannot compute the net diff"):
        await task_scope.collect_net_commit_paths_async(["0" * 40], str(tmp_path))


def _commit_files(
    tmp_path: Path, git: Callable[..., str], message: str, files: Mapping[str, str | None]
) -> str:
    """Write (or, for None, delete) each file, commit everything, and return the commit."""
    for name, content in files.items():
        if content is None:
            (tmp_path / name).unlink()
        else:
            (tmp_path / name).write_text(content)
    git("add", "-A")
    git("commit", "-qm", message)
    return git("rev-parse", "HEAD")


async def test_net_commit_paths_skip_a_stranded_original_whose_replay_the_candidate_joins(
    tmp_path: Path,
) -> None:
    """#23261: the original and its rebase replay both carry the task tag and stay linked."""
    git = _git_repo(tmp_path)
    git("checkout", "-qb", "main")
    base = _commit_files(tmp_path, git, "base", {"f.py": "A = 1\n"})
    _commit_files(tmp_path, git, "foreign old base", {"old.py": "OLD = 1\n"})
    original = _commit_files(tmp_path, git, "original", {"f.py": "A = 2\n"})
    git("checkout", "-qb", "rebased", base)
    new_base = _commit_files(tmp_path, git, "foreign new base", {"new.py": "NEW = 1\n"})
    replay = _commit_files(tmp_path, git, "replay", {"f.py": "A = 2\n"})
    git("checkout", "-qb", "joined", new_base)
    sibling = _commit_files(tmp_path, git, "sibling", {"g.py": "G = 1\n"})
    git("merge", "-q", "--no-ff", "-m", "join", replay)
    join = git("rev-parse", "HEAD")
    linked = [original, sibling, replay, join]

    net = await task_scope.collect_net_commit_paths_async(linked, str(tmp_path), candidate=join)

    assert net == task_scope.NetCommitPaths(
        changed=frozenset({"f.py", "g.py"}), undelivered=(original,)
    )
    with pytest.raises(RuntimeError, match="Cannot compute the net diff"):
        await task_scope.collect_net_commit_paths_async(linked, str(tmp_path))


async def test_net_commit_paths_skip_originals_an_inexact_rebase_left_behind(
    tmp_path: Path,
) -> None:
    """#23076: a conflict-resolved replay and a pin bump the rebase dropped stay linked."""
    git = _git_repo(tmp_path)
    git("checkout", "-qb", "main")
    base = _commit_files(tmp_path, git, "base", {"f.py": "A = 1\nB = 1\n", "pins.toml": "v = 1\n"})
    original = _commit_files(tmp_path, git, "original", {"f.py": "A = 2\nB = 1\n"})
    dropped_bump = _commit_files(tmp_path, git, "pin bump", {"pins.toml": "v = 2\n"})
    git("checkout", "-qb", "rebased", base)
    _commit_files(tmp_path, git, "foreign", {"f.py": "A = 1\nB = 3\n"})
    replay = _commit_files(tmp_path, git, "inexact replay", {"f.py": "A = 2\nB = 3\n"})
    candidate = _commit_files(tmp_path, git, "follow-up", {"h.py": "H = 1\n"})
    linked = [original, dropped_bump, replay, candidate]

    net = await task_scope.collect_net_commit_paths_async(
        linked, str(tmp_path), candidate=candidate
    )

    assert net == task_scope.NetCommitPaths(
        changed=frozenset({"f.py", "h.py"}), undelivered=(original, dropped_bump)
    )


@pytest.mark.parametrize("foreign_via_sync_merge", [False, True])
async def test_net_commit_paths_take_each_links_last_status_over_interleaved_foreign_edits(
    tmp_path: Path, foreign_via_sync_merge: bool
) -> None:
    """#22866 and #23291: a later link rewrites lines an unlinked foreign commit changed.

    No replay onto the pre-task base applies, and a merge-free set's own patches
    are exactly what the task authored, so the paths come from those patches.
    """
    git = _git_repo(tmp_path)
    git("checkout", "-qb", "main")
    base = _commit_files(
        tmp_path, git, "base", {"shared.py": "a\nb\nc\nd\n", "gone.py": "GONE = 1\n"}
    )
    git("checkout", "-qb", "task", base)
    first = _commit_files(tmp_path, git, "first link", {"task.py": "T = 1\n"})
    foreign_edit = {"shared.py": "a\nB\nc\nd\n"}
    if foreign_via_sync_merge:
        git("checkout", "-q", "main")
        _commit_files(tmp_path, git, "foreign", foreign_edit)
        git("checkout", "-q", "task")
        git("merge", "-q", "--no-ff", "-m", "unlinked sync", "main")
    else:
        _commit_files(tmp_path, git, "foreign", foreign_edit)
    second = _commit_files(
        tmp_path, git, "second link", {"shared.py": "a\nd\n", "task.py": "T = 2\n", "gone.py": None}
    )

    net = await task_scope.collect_net_commit_paths_async([first, second], str(tmp_path))

    assert net == task_scope.NetCommitPaths(
        changed=frozenset({"gone.py", "shared.py", "task.py"}), deleted=frozenset({"gone.py"})
    )


async def test_net_commit_paths_stay_unavailable_when_a_linked_merge_needs_foreign_content(
    tmp_path: Path,
) -> None:
    """A linked merge's first-parent diff can carry unlinked content, so no per-link fallback."""
    git = _git_repo(tmp_path)
    git("checkout", "-qb", "main")
    _commit_files(tmp_path, git, "base", {"base.py": "BASE = 1\n"})
    git("checkout", "-qb", "early")
    started = _commit_files(tmp_path, git, "started", {"early.py": "EARLY = True\n"})
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "land early", "early")
    early = git("rev-parse", "HEAD")
    _commit_files(tmp_path, git, "unlinked foreign", {"probe.py": "PROBE = 5\n"})
    git("checkout", "-qb", "late")
    change = _commit_files(tmp_path, git, "change", {"probe.py": "PROBE = 6\n"})
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "land late", "late")
    late = git("rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="Cannot compute the net diff"):
        await task_scope.collect_net_commit_paths_async(
            [started, early, change, late], str(tmp_path)
        )
