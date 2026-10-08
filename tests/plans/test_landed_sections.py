from __future__ import annotations

import importlib
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.plans.parser import parse_plan
from gobby.plans.semantic_lint import lint_plan_document
from gobby.plans.symbol_targets import (
    INDEX_STALE,
    MISSING_SYMBOL_SCOPE,
    UNRESOLVED_SYMBOL,
    validate_symbol_targets,
)
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.expansion._validate import validate_plan_file
from tests.plans.test_symbol_targets import PROJECT_ID, _hash, _Index, _IndexedFile, _Symbol


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def landed_plan(tmp_path: Path) -> tuple[Path, _Index]:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/large.py").write_text("def run():\n    pass\n" + "value = 1\n" * 850)
    (tmp_path / "src/helpers.py").write_text("value = 1\n")
    (tmp_path / "tests/test_created.py").write_text("def test_created():\n    pass\n")
    plan = tmp_path / "plan.md"
    plan.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** landed-lint

            # Landed Lint

            ## P1: Work
            `kind: framing`

            ### 1.1 Split the module [category: code]
            `kind: deliverable`

            Targets:
            - `src/large.py::run`
            - `src/helpers.py`
            - `tests/test_created.py`

            Split `src/large.py` and move helpers into `src/helpers.py`.

            **Acceptance:**
            - 1.1.1 - Behavior passes. file: `src/large.py`
            - 1.1.2 - Helpers moved. file: `src/helpers.py`
            - 1.1.3 - Tests pass. file: `tests/test_created.py`
            """
        ).lstrip()
    )
    symbols = {"src/large.py": [_Symbol("run")], "tests/test_created.py": [_Symbol("test_created")]}
    index = _Index(
        files={
            path: _IndexedFile(_hash(tmp_path / path), len(symbols.get(path, [])))
            for path in ("src/large.py", "src/helpers.py", "tests/test_created.py")
        },
        symbols=symbols,
    )
    return plan, index


def _task(*, closed: bool = False) -> Task:
    now = datetime.now(UTC)
    return Task(
        id="leaf-id",
        project_id=PROJECT_ID,
        title="Split the module",
        priority=1,
        task_type="task",
        created_at=now,
        updated_at=now,
        seq_num=999,
        category="code",
        labels=[f"covers:landed-lint:1.1:1.1.{item}" for item in (1, 2, 3)],
        closed_at=now if closed else None,
        closed_reason="completed" if closed else None,
    )


def _validate(plan: Path, index: _Index, tasks: list[Task]) -> dict[str, Any]:
    manager = MagicMock(spec=LocalTaskManager)
    manager.list_tasks.return_value = tasks
    return validate_plan_file(
        SimpleNamespace(task_manager=manager),
        plan,
        project_context={"id": PROJECT_ID, "project_path": str(plan.parent)},
        code_index=index,
        require_symbol_validation=True,
    )


@pytest.mark.parametrize("problem", ["manager", "project", "database"])
def test_unavailable_completion_is_distinct_from_target_failures(
    landed_plan: tuple[Path, _Index], problem: str
) -> None:
    plan, index = landed_plan
    manager = MagicMock(spec=LocalTaskManager)
    manager.list_tasks.side_effect = [
        [_task(closed=True)],
        psycopg.OperationalError("lookup unavailable"),
    ]
    result = validate_plan_file(
        SimpleNamespace(),
        plan,
        project_context=(
            None if problem == "project" else {"id": PROJECT_ID, "project_path": str(plan.parent)}
        ),
        task_manager=None if problem == "manager" else manager,
        code_index=index,
        require_symbol_validation=True,
    )
    assert result["valid"] is False
    assert result["condition"] == "completed_section_exemptions_unavailable"
    assert len(result["errors"]) == 1
    assert "completed-section exemptions unavailable" in result["errors"][0]
    assert result["symbol_validation"]["status"] == "skipped"
    assert not index.file_queries


@pytest.mark.parametrize(
    "failure", [PermissionError("sandbox"), psycopg.OperationalError("offline")]
)
def test_cli_reports_unavailable_completion_without_target_errors(
    landed_plan: tuple[Path, _Index], monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    plan, _index = landed_plan
    cli = importlib.import_module("gobby.cli.plans")
    monkeypatch.setattr(cli, "_open_db", MagicMock(side_effect=failure))
    monkeypatch.setattr(
        cli,
        "get_project_context",
        lambda _path: {"id": PROJECT_ID, "project_path": str(plan.parent)},
    )
    result = cli._validate_plan_for_cli(plan, None, mode="standard")
    assert result["valid"] is False
    assert result["condition"] == "completed_section_exemptions_unavailable"
    assert result["errors"] == [
        f"completed-section exemptions unavailable: task database: {failure}"
    ]


@pytest.mark.parametrize("closed", [True, False])
def test_deleted_wildcard_target_only_exempts_completed_section(
    landed_plan: tuple[Path, _Index], closed: bool
) -> None:
    plan, index = landed_plan
    plan.write_text(
        plan.read_text().replace(
            "`src/large.py::run`", "`src/large.py::*` — scope-reason: delete entire module"
        )
    )
    (plan.parent / "src/large.py").unlink()
    index.files.pop((PROJECT_ID, "src/large.py"))
    result = _validate(plan, index, [_task(closed=closed)])
    assert result["valid"] is closed, result
    codes = {issue["code"] for issue in result["symbol_validation"]["issues"]}
    assert (UNRESOLVED_SYMBOL in codes) is not closed


def test_p0_counts_and_validates_lettered_deliverable_targets(
    landed_plan: tuple[Path, _Index],
) -> None:
    plan, index = landed_plan
    plan.write_text(plan.read_text().replace("## P1:", "## P0:").replace("1.1", "T1"))
    task = _task(closed=True)
    task.labels = [label.replace("1.1", "T1") for label in task.labels or []]
    result = _validate(plan, index, [task])
    assert result["valid"] is True, result
    assert result["phase_count"] == 1
    assert result["phases"] == {0: "Work"}
    plan.write_text(plan.read_text().replace("src/large.py::run", "src/large.py::missing"))
    result = _validate(plan, index, [task])
    assert result["valid"] is False
    assert any(
        issue["code"] == UNRESOLVED_SYMBOL and issue["section_id"] == "T1"
        for issue in result["symbol_validation"]["issues"]
    )


@pytest.mark.parametrize("closed", [False, True], ids=["landed-open", "closed"])
def test_landed_or_closed_section_keeps_original_targets(
    landed_plan: tuple[Path, _Index], closed: bool
) -> None:
    plan, index = landed_plan
    before = plan.read_bytes()
    if not closed:
        _git(plan.parent, "init", "-b", "main")
        _git(plan.parent, "add", ".")
        _git(
            plan.parent,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "[gobby-#999] fix: split module",
        )
    result = _validate(plan, index, [_task(closed=closed)])
    assert result["valid"] is closed, result
    if not closed:
        assert "production-size-growth" in str(result["errors"])
    assert plan.read_bytes() == before


def test_unfinished_section_still_rejects_size_and_bare_target(
    landed_plan: tuple[Path, _Index],
) -> None:
    plan, index = landed_plan
    result = _validate(plan, index, [_task()])
    assert result["valid"] is False
    semantic = lint_plan_document(parse_plan(plan, parse_mode="draft"), project_root=plan.parent)
    assert [issue.code for issue in semantic.issues] == ["production-size-growth"]
    symbols = validate_symbol_targets(
        parse_plan(plan, parse_mode="draft"),
        project_context={"id": PROJECT_ID, "project_path": str(plan.parent)},
        code_index=index,
        required=True,
    )
    assert [issue.code for issue in symbols.issues if issue.blocking] == [MISSING_SYMBOL_SCOPE]


@pytest.mark.parametrize("bare_only", [False, True], ids=["size-growth", "bare-target"])
@pytest.mark.parametrize("landed", [False, True], ids=["no-landing", "landed"])
@pytest.mark.parametrize(
    "reason",
    [
        None,
        "unknown",
        "completed",
        "already_implemented",
        "duplicate",
        "wont_fix",
        "obsolete",
        "out_of_repo",
    ],
)
def test_closed_section_requires_delivered_reason_or_landing(
    landed_plan: tuple[Path, _Index], reason: str | None, landed: bool, bare_only: bool
) -> None:
    plan, index = landed_plan
    before = plan.read_bytes()
    if bare_only:
        source = plan.parent / "src/large.py"
        source.write_text("def run():\n    pass\n")
        index.files[(PROJECT_ID, "src/large.py")] = _IndexedFile(_hash(source), 1)
    if landed:
        _git(plan.parent, "init", "-b", "main")
        _git(plan.parent, "add", ".")
        _git(
            plan.parent,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "[gobby-#999] fix: split module",
        )
    task = _task(closed=True)
    task.closed_reason = reason
    result = _validate(plan, index, [task])
    delivered = reason in {"completed", "already_implemented"}
    abandoned = reason in {"duplicate", "wont_fix", "obsolete", "out_of_repo"}
    expected = not abandoned and (delivered or (landed and bare_only))
    assert result["valid"] is expected, result
    if not expected:
        assert (MISSING_SYMBOL_SCOPE if bare_only else "production-size-growth") in str(result)
    assert plan.read_bytes() == before


@pytest.mark.parametrize("problem", ["missing", "partial", "duplicate", "foreign", "abandoned"])
def test_completion_evidence_fails_closed(landed_plan: tuple[Path, _Index], problem: str) -> None:
    plan, index = landed_plan
    task = _task(closed=True)
    tasks = [task]
    if problem == "missing":
        tasks = []
    elif problem == "partial":
        task.labels = task.labels[:1] if task.labels else []
    elif problem == "duplicate":
        tasks.append(_task(closed=True))
    elif problem == "foreign":
        task.project_id = "other-project"
    else:
        task.closed_reason = "wont_fix"
    result = _validate(plan, index, tasks)
    assert result["valid"] is False
    expected_error = MISSING_SYMBOL_SCOPE if problem == "duplicate" else "production-size-growth"
    assert expected_error in str(result)


def test_unmerged_worktree_commit_is_not_landed(
    landed_plan: tuple[Path, _Index], tmp_path: Path
) -> None:
    plan, index = landed_plan
    _git(plan.parent, "init", "-b", "main")
    _git(plan.parent, "add", ".")
    _git(
        plan.parent,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "baseline",
    )
    worktree = tmp_path / "candidate"
    _git(plan.parent, "worktree", "add", "-b", "candidate", str(worktree))
    (worktree / "src/helpers.py").write_text("value = 2\n")
    _git(worktree, "add", ".")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "[gobby-#999] fix: unmerged split",
    )
    result = _validate(worktree / "plan.md", index, [_task()])
    assert result["valid"] is False
    assert "production-size-growth" in str(result["errors"])


@pytest.mark.parametrize("issue_code", [INDEX_STALE, UNRESOLVED_SYMBOL])
def test_completed_section_still_checks_symbols(
    landed_plan: tuple[Path, _Index], issue_code: str
) -> None:
    plan, index = landed_plan
    if issue_code == INDEX_STALE:
        (plan.parent / "tests/test_created.py").write_text("def changed():\n    pass\n")
    else:
        plan.write_text(plan.read_text().replace("src/large.py::run", "src/large.py::missing"))
    result = _validate(plan, index, [_task(closed=True)])
    assert result["valid"] is False
    assert issue_code in str(result["symbol_validation"])


@pytest.mark.parametrize("closed", [False, True], ids=["unfinished", "closed"])
def test_bare_target_completion_is_scoped_to_its_section(
    landed_plan: tuple[Path, _Index], closed: bool
) -> None:
    plan, index = landed_plan
    source = plan.parent / "src/large.py"
    source.write_text("def run():\n    pass\n")
    index.files[(PROJECT_ID, "src/large.py")] = _IndexedFile(_hash(source), 1)
    result = _validate(plan, index, [_task(closed=closed)])
    assert result["valid"] is closed
    if not closed:
        assert MISSING_SYMBOL_SCOPE in str(result["symbol_validation"])


def test_cli_rechecks_size_growth_with_completion_evidence(
    landed_plan: tuple[Path, _Index], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, index = landed_plan
    before = plan.read_bytes()
    cli = importlib.import_module("gobby.cli.plans")
    manager = MagicMock(spec=LocalTaskManager)
    manager.list_tasks.return_value = [_task(closed=True)]
    open_db = MagicMock(return_value=object())
    monkeypatch.setattr(cli, "_open_db", open_db)
    monkeypatch.setattr(cli, "LocalTaskManager", lambda _db: manager)
    monkeypatch.setattr(cli, "CodeIndexStorage", lambda _db: index)
    monkeypatch.setattr(
        cli,
        "get_project_context",
        lambda _path: {"id": PROJECT_ID, "project_path": str(plan.parent)},
    )
    result = cli._validate_plan_for_cli(plan, None, mode="standard")
    assert result["valid"] is True, result
    assert result["symbol_validation"]["status"] == "passed"
    assert result["symbol_validation"]["checked_targets"] == [
        "src/large.py::run",
        "src/helpers.py",
        "tests/test_created.py",
    ]
    assert result["symbol_validation"]["checked_symbols"] == ["src/large.py::run"]
    assert plan.read_bytes() == before
    open_db.assert_called_once_with()
    assert manager.list_tasks.call_count == 3
    for item, call in enumerate(manager.list_tasks.call_args_list, 1):
        assert call.kwargs == {
            "project_id": PROJECT_ID,
            "label": f"covers:landed-lint:1.1:1.1.{item}",
            "limit": 50,
            "offset": 0,
            "sort_by": "created_at",
        }


def test_duplicate_owner_on_later_acceptance_item_is_not_completion(
    landed_plan: tuple[Path, _Index],
) -> None:
    plan, index = landed_plan
    task = _task(closed=True)
    manager = MagicMock(spec=LocalTaskManager)
    manager.list_tasks.side_effect = [[task], [task, _task(closed=True)], [task]]
    result = validate_plan_file(
        SimpleNamespace(task_manager=manager),
        plan,
        project_context={"id": PROJECT_ID, "project_path": str(plan.parent)},
        code_index=index,
        require_symbol_validation=True,
    )
    assert result["valid"] is False
    assert MISSING_SYMBOL_SCOPE in str(result)


@pytest.mark.parametrize("repair_closed", [False, True], ids=["repair-open", "all-closed"])
def test_size_growth_requires_every_covering_leaf_closed(
    landed_plan: tuple[Path, _Index], repair_closed: bool
) -> None:
    plan, index = landed_plan
    plan.write_text(
        plan.read_text().replace("`tests/test_created.py`", "`tests/test_created.py::test_created`")
    )
    original = _task(closed=True)
    repair = _task(closed=repair_closed)
    repair.id = "repair-id"
    repair.labels = ["covers:landed-lint:1.1:1.1.2"]
    manager = MagicMock(spec=LocalTaskManager)

    def covering_tasks(*, label: str, **kwargs: Any) -> list[Task]:
        return [task for task in (original, repair) if label in (task.labels or [])]

    manager.list_tasks.side_effect = covering_tasks
    result = validate_plan_file(
        SimpleNamespace(task_manager=manager),
        plan,
        project_context={"id": PROJECT_ID, "project_path": str(plan.parent)},
        code_index=index,
        require_symbol_validation=True,
    )
    growth_issues = [
        error for error in result.get("errors", []) if "production-size-growth" in error
    ]
    assert bool(growth_issues) is not repair_closed, result
    assert result["valid"] is repair_closed, result


@pytest.mark.parametrize("last_closed", [False, True], ids=["late-open", "all-pages-closed"])
def test_size_growth_checks_covering_leaves_beyond_first_page(
    landed_plan: tuple[Path, _Index], last_closed: bool
) -> None:
    plan, index = landed_plan
    plan.write_text(
        plan.read_text().replace("`tests/test_created.py`", "`tests/test_created.py::test_created`")
    )
    tasks = [_task(closed=True) for _ in range(51)]
    for number, task in enumerate(tasks):
        task.id = f"leaf-{number}"
    tasks[-1].closed_at = datetime.now(UTC) if last_closed else None
    tasks[-1].closed_reason = "completed" if last_closed else None
    manager = MagicMock(spec=LocalTaskManager)

    def covering_tasks(*, offset: int, limit: int, **kwargs: Any) -> list[Task]:
        return tasks[offset : offset + limit]

    manager.list_tasks.side_effect = covering_tasks
    result = validate_plan_file(
        SimpleNamespace(task_manager=manager),
        plan,
        project_context={"id": PROJECT_ID, "project_path": str(plan.parent)},
        code_index=index,
        require_symbol_validation=True,
    )
    growth_issues = [
        error for error in result.get("errors", []) if "production-size-growth" in error
    ]
    assert bool(growth_issues) is not last_closed, result
    assert result["valid"] is last_closed, result
