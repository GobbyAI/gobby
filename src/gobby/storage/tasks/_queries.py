"""Task query operations.

This module provides query operations for listing and filtering tasks:
- list_tasks: General task listing with filters
- list_ready_tasks: Tasks ready to work on (not blocked)
- list_blocked_tasks: Tasks blocked by dependencies
- list_workflow_tasks: Tasks associated with a workflow
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.hub.protocol import HubDatabase, Row, Transaction
from gobby.storage.sql_dialect import json_array_contains_condition
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._models import Task, task_type_filter_values
from gobby.storage.tasks._ordering import TaskOrderKey, order_task_keys
from gobby.storage.tasks._read import _escape_like_pattern
from gobby.storage.tasks._stage_hydration import hydrate_task_stage_state


def _current_stage_state_filter_sql(
    current_stage_state: str | list[str] | None,
) -> tuple[str | None, list[Any]]:
    """Build a current-stage-state filter clause and params."""
    if not current_stage_state:
        return None, []

    raw_values = (
        [current_stage_state] if isinstance(current_stage_state, str) else list(current_stage_state)
    )
    normalized_values = [
        str(value).strip().lower().replace("-", "_") for value in raw_values if str(value).strip()
    ]
    if not normalized_values:
        return None, []

    placeholders = ", ".join("%s" for _ in normalized_values)
    clauses = [
        f"""
        EXISTS (
            SELECT 1
              FROM task_stage_states current_stage_filter
             WHERE current_stage_filter.task_id = tasks.id
               AND current_stage_filter.state != 'done'
               AND current_stage_filter.position = (
                   SELECT MIN(stage_scan.position)
                     FROM task_stage_states stage_scan
                    WHERE stage_scan.task_id = tasks.id
                      AND stage_scan.state != 'done'
               )
               AND current_stage_filter.state IN ({placeholders})
        )
        """
    ]
    params = list(normalized_values)
    if "ready" in normalized_values:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                  FROM task_stage_states stage_any
                 WHERE stage_any.task_id = tasks.id
            )
            """
        )
    return f"({' OR '.join(clauses)})", params


def _current_stage_join_sql(task_alias: str = "t", *, join_type: str = "LEFT JOIN") -> str:
    return f"""
    {join_type} task_stage_states current_stage
      ON current_stage.task_id = {task_alias}.id
     AND current_stage.state != 'done'
     AND current_stage.position = (
         SELECT MIN(stage_scan.position)
           FROM task_stage_states stage_scan
          WHERE stage_scan.task_id = {task_alias}.id
            AND stage_scan.state != 'done'
     )
    """


def _not_closed_or_escalated_sql(task_alias: str = "t") -> str:
    return (
        f"{task_alias}.closed_at IS NULL "
        f"AND {task_alias}.escalated_at IS NULL "
        f"AND COALESCE({task_alias}.is_escalated, FALSE) IS FALSE"
    )


def _external_blocker_exists_sql(task_alias: str = "t") -> str:
    return f"""
    EXISTS (
        SELECT 1 FROM task_dependencies d
        JOIN tasks blocker ON d.depends_on = blocker.id
        WHERE d.task_id = {task_alias}.id
          AND d.dep_type = 'blocks'
          AND blocker.closed_at IS NULL
          AND NOT EXISTS (
              WITH RECURSIVE ancestors(ancestor_id, path, depth) AS (
                  SELECT blocker.parent_task_id,
                         ARRAY[blocker.id, blocker.parent_task_id],
                         1
                  WHERE blocker.parent_task_id IS NOT NULL
                  UNION ALL
                  SELECT p.parent_task_id, a.path || p.parent_task_id, a.depth + 1
                  FROM tasks p
                  JOIN ancestors a ON p.id = a.ancestor_id
                  WHERE p.parent_task_id IS NOT NULL
                    AND a.depth < 100
                    AND NOT p.parent_task_id = ANY(a.path)
              )
              SELECT 1 FROM ancestors WHERE ancestor_id = {task_alias}.id
          )
    )
    """


def _no_external_blocker_sql(task_alias: str = "t") -> str:
    return f"NOT {_external_blocker_exists_sql(task_alias)}"


def list_tasks(
    db: HubDatabase,
    project_id: str | None = None,
    current_stage_state: str | list[str] | None = None,
    priority: int | None = None,
    claimed_by_session_id: str | None = None,
    claimed: bool | None = None,
    closed: bool | None = None,
    escalated: bool | None = None,
    task_type: str | None = None,
    label: str | None = None,
    parent_task_id: str | None = None,
    title_like: str | None = None,
    stages: list[str] | None = None,
    stage_state: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "hierarchy",
    sort_order: str = "asc",
) -> list[Task]:
    """List tasks with filtering.

    Args:
        db: Database protocol instance
        project_id: Filter by project
        current_stage_state: Filter by the task's current stage state.
        priority: Filter by priority
        claimed_by_session_id: Filter by canonical owning session
        claimed: Filter by whether canonical ownership exists
        closed: Filter by canonical closed state
        escalated: Filter by escalated state
        task_type: Filter by task type
        label: Filter by label
        parent_task_id: Filter by parent task
        title_like: Filter by title (partial match)
        limit: Maximum tasks to return
        offset: Pagination offset
        sort_by: Ordering strategy. "hierarchy" preserves parent/child ordering;
            "updated_at" and "created_at" sort chronologically; "priority"
            sorts by task priority (lower value = higher priority).
        sort_order: "asc" or "desc" for non-hierarchical sorts.

    Results are ordered hierarchically: parents appear before their children;
    roots and siblings are ordered by priority ASC then created_at ASC; and
    siblings that block one another are ordered topologically first, so a
    blocker precedes what it blocks even when its own key sorts it later. That
    contract is unchanged -- #20840 made the ordering read a four-column
    projection instead of whole task rows, which is why a page no longer costs
    the whole project.
    """
    where = "1=1"
    params: list[Any] = []

    if project_id:
        where += " AND project_id = %s"
        params.append(project_id)
    if current_stage_state:
        clause, clause_params = _current_stage_state_filter_sql(current_stage_state)
        if clause:
            where += f" AND {clause}"
            params.extend(clause_params)
            if closed is None:
                where += " AND closed_at IS NULL"
    if priority is not None:
        where += " AND priority = %s"
        params.append(priority)
    if claimed_by_session_id:
        where += " AND claimed_by_session_id = %s"
        params.append(claimed_by_session_id)
    if claimed is True:
        where += " AND claimed_by_session_id IS NOT NULL"
    elif claimed is False:
        where += " AND claimed_by_session_id IS NULL"
    if closed is True:
        where += " AND closed_at IS NOT NULL"
    elif closed is False:
        where += " AND closed_at IS NULL"
    if escalated is True:
        where += " AND COALESCE(is_escalated, FALSE) IS TRUE"
    elif escalated is False:
        where += " AND COALESCE(is_escalated, FALSE) IS FALSE"
    if task_type:
        task_type_values = task_type_filter_values(task_type)
        placeholders = ", ".join("%s" for _ in task_type_values)
        where += f" AND task_type IN ({placeholders})"
        params.extend(task_type_values)
    if label:
        label_clause, label_params = json_array_contains_condition(db, "tasks.labels", label)
        where += f" AND {label_clause}"
        params.extend(label_params)
    if parent_task_id:
        where += " AND parent_task_id = %s"
        params.append(parent_task_id)
    if title_like:
        where += " AND title LIKE %s ESCAPE '\\'"
        params.append(f"%{_escape_like_pattern(title_like)}%")
    if stages:
        placeholders = ", ".join("%s" for _ in stages)
        where += f"""
        AND EXISTS (
            SELECT 1
              FROM task_stage_states stage_filter
             WHERE stage_filter.task_id = tasks.id
               AND stage_filter.stage_name IN ({placeholders})
        """
        params.extend(stages)
        if stage_state is not None:
            where += " AND stage_filter.state = %s"
            params.append(stage_state)
        where += ")"

    if sort_by == "hierarchy":
        return _hierarchy_page(db, where, params, limit=limit, offset=offset)

    valid_sorts = {
        "updated_at": "updated_at",
        "created_at": "created_at",
        "priority": "priority",
    }
    if sort_by not in valid_sorts:
        # sort_by arrives unvalidated from ?sort_by=, and the route turns a
        # ValueError into a 400. A KeyError would reach the client as a 500.
        raise ValueError(
            f"Unknown sort_by {sort_by!r}; expected one of: hierarchy, "
            + ", ".join(sorted(valid_sorts))
        )
    order_clause = valid_sorts[sort_by]
    direction = "DESC" if sort_order.lower() == "desc" else "ASC"
    query = (
        f"SELECT * FROM tasks WHERE {where}"
        f" ORDER BY {order_clause} {direction}, priority ASC, created_at DESC, id ASC "
        "LIMIT %s OFFSET %s"
    )
    params.extend([limit, offset])

    rows = db.fetchall(query, tuple(params))
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_stage_state(db, tasks)
    hydrate_task_blocking_state(db, tasks)
    return tasks


def _hierarchy_page(
    db: HubDatabase,
    where: str,
    params: list[Any],
    *,
    limit: int,
    offset: int,
) -> list[Task]:
    """Order the whole filtered set, then hydrate only the requested page.

    Hierarchy order is global: which tasks land on a page of 20 cannot be known
    without ordering everything the filter matches, so there is no SQL LIMIT to
    push down. What can be avoided is paying for the rows the page discards.
    Ordering reads four columns and the local blocking edges; fetching whole
    task rows, converting them, and hydrating their stage and blocking state
    for a project-sized set is work thrown away for all but ``limit`` of them.

    Measured on a 14,904-task project: SELECT * of every row cost 331.8 ms plus
    89.5 ms of Task.from_row and 145.1 ms of hydration, against 48.9 ms for the
    projection and 15.3 ms for the edges. The per-row Python is what mattered
    most -- under four concurrent listings the loop thread spent 95.5% of its
    samples in take_gil, because that conversion runs on the shared db executor
    (#20840, #20846).

    All five reads share one snapshot. Three independent transactions let a
    delete land between the projection and the page fetch, which drops the
    vanished ids and silently shortens the page while the caller's separate
    ``count_tasks`` still reports the old total (#20870 F2). Callers that want
    the page and the total to agree open their own snapshot around both; this
    one is inherited.
    """
    with task_read_snapshot(db):
        # order_task_keys breaks (priority, created_at) ties by input order, so
        # without this ORDER BY the tie-break is Postgres heap order. Ties are
        # the common case -- priority defaults to 2 and created_at is the
        # transaction timestamp, so siblings applied in one expansion carry a
        # byte-identical key -- and a non-HOT update between two pages would
        # then show a task twice or not at all (#20870 F1).
        key_rows = db.fetchall(
            f"SELECT id, parent_task_id, priority, created_at FROM tasks WHERE {where}"
            " ORDER BY priority ASC, created_at ASC, id ASC",
            tuple(params),
        )
        if not key_rows:
            return []

        # Only edges whose blocked end is in the filtered set can reorder it, and
        # order_task_keys ignores blockers outside a task's own sibling group.
        # Re-embedding the WHERE evaluates the filter a second time; substituting
        # the ids already in hand ships one UUID per matching row instead.
        # Measured on this 14,920-task project (#20878): a stage-filtered
        # listing pays 2.2 ms embedded vs 1.2 ms for ids in hand, while the
        # plain project listing pays 7.7 ms embedded vs 22.0 ms shipping all
        # 14,920 ids. The unselective shape is the common one, so the embedded
        # WHERE stays.
        edge_rows = db.fetchall(
            "SELECT task_id, depends_on FROM task_dependencies"
            f" WHERE dep_type = 'blocks' AND task_id IN (SELECT id FROM tasks WHERE {where})",
            tuple(params),
        )
        return _hydrated_page(
            db, key_rows, _blockers_by_task(edge_rows), limit=limit, offset=offset
        )


def _blockers_by_task(edge_rows: list[Row]) -> dict[str, list[str]]:
    """Group blocking-edge rows by the task they block."""
    blockers: dict[str, list[str]] = {}
    for edge in edge_rows:
        blockers.setdefault(str(edge["task_id"]), []).append(str(edge["depends_on"]))
    return blockers


def _hydrated_page(
    db: HubDatabase,
    key_rows: list[Row],
    blockers: dict[str, list[str]],
    *,
    limit: int,
    offset: int,
) -> list[Task]:
    """Order projected keys, slice the page, then fetch and hydrate only it.

    The tail every hierarchy-ordered listing shares: the caller supplies the
    key projection and the blocking edges, this converts and hydrates nothing
    beyond the ``limit`` tasks it returns. Runs inside the caller's snapshot so
    the page fetch and hydration see the projection's view of the tasks.
    """
    keys = [
        TaskOrderKey(
            id=str(row["id"]),
            parent_task_id=str(row["parent_task_id"]) if row["parent_task_id"] else None,
            priority=row["priority"],
            created_at=row["created_at"],
            blocked_by=tuple(blockers.get(str(row["id"]), ())),
        )
        for row in key_rows
    ]
    ordered_ids = order_task_keys(keys)
    page_ids = ordered_ids[offset : offset + limit] if limit else ordered_ids[offset:]
    if not page_ids:
        return []

    rows = db.fetchall("SELECT * FROM tasks WHERE id = ANY(%s)", (page_ids,))
    by_id = {str(row["id"]): Task.from_row(row) for row in rows}
    # ANY() does not preserve argument order, so re-impose the page's own.
    tasks = [by_id[task_id] for task_id in page_ids if task_id in by_id]
    hydrate_task_stage_state(db, tasks)
    hydrate_task_blocking_state(db, tasks)
    return tasks


def _projected_listing_page(
    db: HubDatabase,
    projection_query: str,
    params: list[Any],
    *,
    limit: int,
    offset: int,
) -> list[Task]:
    """Run a key-projection listing and return one hydrated page from it.

    The ready and blocked listings' filters are the expensive half of their
    queries -- a recursive readiness CTE and a recursive ancestor check -- so
    unlike ``_hierarchy_page`` the edge fetch substitutes the ids already in
    hand rather than evaluating the filter a second time. All reads share one
    snapshot for the same reason ``_hierarchy_page``'s do (#20870 F2).
    """
    with task_read_snapshot(db):
        key_rows = db.fetchall(projection_query, tuple(params))
        if not key_rows:
            return []

        edge_rows = db.fetchall(
            "SELECT task_id, depends_on FROM task_dependencies"
            " WHERE dep_type = 'blocks' AND task_id = ANY(%s)",
            ([str(row["id"]) for row in key_rows],),
        )
        return _hydrated_page(
            db, key_rows, _blockers_by_task(edge_rows), limit=limit, offset=offset
        )


@contextmanager
def task_read_snapshot(db: HubDatabase) -> Iterator[Transaction]:
    """Hold one read-only snapshot for a whole task read.

    ``SET TRANSACTION ISOLATION LEVEL`` is only legal as a transaction's first
    statement, so a read running inside a caller's transaction reads that
    caller's view rather than redeclaring one. Nested ``db.fetchall`` calls
    join whichever transaction is ambient, which is what keeps the hydration
    helpers on the snapshot without threading a connection through them.

    Wrap a page and its total together to make them agree; the hierarchy page
    inherits the snapshot rather than opening a second one.
    """
    ambient = ambient_transaction(db)
    if ambient is not None:
        yield ambient
        return
    with db.transaction() as transaction:
        transaction.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield transaction


def _ready_tasks_cte_sql() -> str:
    """Build the CTE shared by ready-task list and count queries."""
    return f"""
    WITH RECURSIVE ready_tasks(id, path, depth) AS (
        -- Base case: open/in_progress tasks with no parent and no external blocking deps
        SELECT t.id, ARRAY[t.id], 0 FROM tasks t
        {_current_stage_join_sql("t")}
        WHERE {_not_closed_or_escalated_sql("t")}
        AND (
            current_stage.state IN ('ready', 'in_progress')
            OR NOT EXISTS (
                SELECT 1 FROM task_stage_states stage_any WHERE stage_any.task_id = t.id
            )
        )
        AND t.parent_task_id IS NULL
        AND {_no_external_blocker_sql("t")}

        UNION ALL

        -- Recursive case: open/in_progress tasks whose parent is ready and no external blocking deps
        SELECT t.id, rt.path || t.id, rt.depth + 1 FROM tasks t
        JOIN ready_tasks rt ON t.parent_task_id = rt.id
        {_current_stage_join_sql("t")}
        WHERE {_not_closed_or_escalated_sql("t")}
        AND (
            current_stage.state IN ('ready', 'in_progress')
            OR NOT EXISTS (
                SELECT 1 FROM task_stage_states stage_any WHERE stage_any.task_id = t.id
            )
        )
        AND {_no_external_blocker_sql("t")}
        AND rt.depth < 100
        AND NOT t.id = ANY(rt.path)
    )
    """


def list_ready_tasks(
    db: HubDatabase,
    project_id: str | None = None,
    priority: int | None = None,
    task_type: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    """List tasks that are ready to work on (open or in_progress) and not blocked.

    A task is ready if:
    1. It is open or in_progress
    2. It has no open blocking dependencies
    3. Its parent (if any) is also ready (recursive check up the chain)

    Note: in_progress tasks are included because they represent active work
    that should remain visible in the ready queue.

    Results are ordered hierarchically: parents appear before their children,
    with siblings sorted by priority ASC, then created_at ASC.

    Note: The limit is applied AFTER hierarchical ordering to ensure coherent
    tree structures, so ordering must see every matching task -- but it needs
    only the four-column key projection, not whole rows. This listing backs
    suggest_next_task and the dispatcher, so it pages the way #20840 taught
    the main listing to: order over the projection, then fetch, convert, and
    hydrate only the page (#20878).
    """
    # Use recursive CTE to find tasks with ready parent chains. The ORDER BY
    # makes the (priority, created_at) tie-break deterministic instead of
    # Postgres heap order -- order_task_keys breaks ties by input order, and
    # ties are the common case (#20870 F1).
    query = f"""
    {_ready_tasks_cte_sql()}
    SELECT t.id, t.parent_task_id, t.priority, t.created_at FROM tasks t
    JOIN ready_tasks rt ON t.id = rt.id
    WHERE 1=1
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = %s"
        params.append(project_id)
    if priority is not None:
        query += " AND t.priority = %s"
        params.append(priority)
    if task_type:
        query += " AND t.task_type = %s"
        params.append(task_type)
    if parent_task_id:
        query += " AND t.parent_task_id = %s"
        params.append(parent_task_id)

    query += " ORDER BY t.priority ASC, t.created_at ASC, t.id ASC"

    return _projected_listing_page(db, query, params, limit=limit, offset=offset)


def list_blocked_tasks(
    db: HubDatabase,
    project_id: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    """List tasks that are blocked by at least one open blocking dependency.

    Only considers "external" blockers - excludes parent tasks being blocked
    by their own descendants (which is a "completion" block, not a "work" block).

    Results are ordered hierarchically: parents appear before their children,
    with siblings sorted by priority ASC, then created_at ASC.

    Note: The limit is applied AFTER hierarchical ordering to ensure coherent
    tree structures, so ordering must see every matching task -- over the
    four-column key projection, with only the returned page fetched,
    converted, and hydrated (#20878, same shape as #20840's main listing).
    """
    # The ORDER BY makes the (priority, created_at) tie-break deterministic
    # instead of Postgres heap order (#20870 F1).
    query = f"""
    SELECT t.id, t.parent_task_id, t.priority, t.created_at FROM tasks t
    WHERE t.closed_at IS NULL
    AND (
        t.escalated_at IS NOT NULL
        OR COALESCE(t.is_escalated, FALSE) IS TRUE
        OR {_external_blocker_exists_sql("t")}
    )
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = %s"
        params.append(project_id)
    if parent_task_id:
        query += " AND t.parent_task_id = %s"
        params.append(parent_task_id)

    query += " ORDER BY t.priority ASC, t.created_at ASC, t.id ASC"

    return _projected_listing_page(db, query, params, limit=limit, offset=offset)
