"""Time the epic-guard task fetch against the live hub database (#20847).

`collect_epic_guard_paths` used to page `LocalTaskManager.list_tasks` 500 rows
at a time until the whole project was in memory; it now issues one recursive
CTE for the task's ancestors plus its nearest epic ancestor's subtree. Both are
timed here against the same project and the same task so the numbers are
comparable, and the row counts show what the scoping actually removed.

Usage:

    uv run python scripts/bench_epic_guard_scope.py '#20847'
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from gobby.config.bootstrap import load_bootstrap
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.epic_guards import collect_epic_guard_paths
from gobby.utils.project_context import get_project_context

_PAGE = 500


def page_every_project_task(manager: LocalTaskManager, project_id: str) -> list[Task]:
    """The implementation #20847 replaced, reproduced for comparison."""
    tasks: list[Task] = []
    offset = 0
    while True:
        page = manager.list_tasks(project_id=project_id, limit=_PAGE, offset=offset)
        tasks.extend(page)
        if len(page) < _PAGE:
            return tasks
        offset += len(page)


def timed[T](label: str, call: Callable[[], T]) -> tuple[float, T]:
    start = time.perf_counter()
    result = call()
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"  {label:<44} {elapsed_ms:9.1f} ms")
    return elapsed_ms, result


def main(task_ref: str, repo_path: str) -> int:
    config = load_bootstrap(resolve_database_url=True)
    if not config.database_url:
        print("PostgreSQL hub database is not configured", file=sys.stderr)
        return 1
    db = PostgresHubDatabase(config.database_url, pool_config=config.postgres_pool)
    manager = LocalTaskManager(db)

    project = get_project_context(Path(repo_path))
    if project is None:
        print(f"{repo_path} is not an initialized gobby project", file=sys.stderr)
        return 1

    task_id = manager.resolve_task_reference(task_ref, project["id"])
    task = manager.get_task(task_id)
    if task is None:
        print(f"no such task {task_ref}", file=sys.stderr)
        return 1
    print(f"task {task_ref} ({task.title[:48]}) in project {task.project_id}\n")

    print("row fetch:")
    before_ms, before_rows = timed(
        "BEFORE  page every task in the project",
        lambda: page_every_project_task(manager, task.project_id),
    )
    after_ms, after_rows = timed(
        "AFTER   scoped ancestors + epic subtree",
        lambda: manager.list_epic_guard_scope(task.id),
    )
    print(f"\n  rows: {len(before_rows)} -> {len(after_rows)}")
    print(f"  speedup: {before_ms / after_ms:.0f}x\n")

    print("full guard collection (new path):")
    _, collected = timed(
        "collect_epic_guard_paths",
        lambda: collect_epic_guard_paths(task_manager=manager, task=task, repo_path=repo_path),
    )
    paths, sources, errors = collected
    print(f"\n  paths={len(paths)} sources={len(sources)} errors={len(errors)}")
    for path in paths[:6]:
        print(f"    {path}")
    return 0


if __name__ == "__main__":
    ref = sys.argv[1] if len(sys.argv) > 1 else "#20847"
    root = sys.argv[2] if len(sys.argv) > 2 else str(Path.cwd())
    sys.exit(main(ref, root))
