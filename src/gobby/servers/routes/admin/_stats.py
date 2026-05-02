"""Time-filtered dashboard statistics endpoint."""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def _current_stage_join_sql(task_alias: str = "task") -> str:
    return f"""
    LEFT JOIN task_stage_states current_stage
      ON current_stage.task_id = {task_alias}.id
     AND current_stage.state != 'done'
     AND current_stage.position = (
         SELECT MIN(stage_scan.position)
           FROM task_stage_states stage_scan
          WHERE stage_scan.task_id = {task_alias}.id
            AND stage_scan.state != 'done'
     )
    """


def _task_state_bucket_sql(task_alias: str = "task") -> str:
    return (
        "CASE "
        f"WHEN {task_alias}.closed_at IS NOT NULL THEN 'closed' "
        f"WHEN {task_alias}.escalated_at IS NOT NULL "
        f"OR COALESCE({task_alias}.is_escalated, 0) = 1 THEN 'escalated' "
        "WHEN current_stage.state = 'ready' THEN 'open' "
        "WHEN current_stage.state IN ('in_progress', 'needs_review', 'review_approved') "
        "THEN current_stage.state "
        "ELSE 'open' END"
    )


def _not_closed_or_escalated_sql(task_alias: str = "t") -> str:
    return (
        f"{task_alias}.closed_at IS NULL "
        f"AND {task_alias}.escalated_at IS NULL "
        f"AND COALESCE({task_alias}.is_escalated, 0) = 0"
    )


def _build_filters(
    hours: int | None,
    days: int,
    project_id: str | None,
    *,
    created_col: str = "created_at",
    project_col: str = "project_id",
) -> tuple[str, list[str]]:
    """Build SQL WHERE fragments and params for time + project filtering."""
    clauses: list[str] = []
    params: list[str] = []

    # Time filter: hours takes precedence over days
    if hours is not None and hours > 0:
        clauses.append(f"AND {created_col} >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
        params.append(f"-{hours} hours")
    elif hours is None and days > 0:
        clauses.append(f"AND {created_col} >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
        params.append(f"-{days} days")
    # hours=0 or days=0 means all time — no filter

    if project_id:
        clauses.append(f"AND {project_col} = ?")
        params.append(project_id)

    return " ".join(clauses), params


def register_stats_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/stats")
    async def get_stats(
        days: int = Query(0, ge=0, le=36500),
        hours: int | None = Query(None, ge=0, le=8760),
        project_id: str | None = Query(None),
    ) -> dict[str, Any]:
        """Get time-filtered counts for tasks, sessions, and memory.

        Args:
            days: Time window in days. 0 = all time. Ignored when hours is set.
            hours: Time window in hours. 0 = all time. Takes precedence over days.
            project_id: Filter to a specific project.
        """
        db = server.services.database
        time_filter, params = _build_filters(hours, days, project_id)

        # --- Tasks ---
        task_stats: dict[str, Any] = {
            "open": 0,
            "in_progress": 0,
            "closed": 0,
            "needs_review": 0,
            "review_approved": 0,
            "escalated": 0,
            "ready": 0,
            "blocked": 0,
            "closed_24h": 0,
        }
        try:
            state_bucket = _task_state_bucket_sql("task")
            rows = db.fetchall(
                f"SELECT {state_bucket} as status, COUNT(*) as cnt FROM tasks task "
                f"{_current_stage_join_sql('task')} "
                f"WHERE 1=1 {time_filter} GROUP BY status",
                tuple(params),
            )
            for row in rows:
                status = row["status"]
                if status in task_stats:
                    task_stats[status] = row["cnt"]

            # Ready = canonically executable tasks with no unresolved blocking deps
            tf_aliased, _ = _build_filters(
                hours,
                days,
                project_id,
                created_col="t.created_at",
                project_col="t.project_id",
            )
            ready_rows = db.fetchall(
                "SELECT COUNT(*) as cnt FROM tasks t "
                f"{_current_stage_join_sql('t')} "
                f"WHERE {_not_closed_or_escalated_sql('t')} "
                "AND current_stage.state IN ('ready', 'in_progress') "
                f"{tf_aliased} "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_dependencies td "
                "  JOIN tasks blocker ON td.depends_on = blocker.id "
                "  WHERE td.task_id = t.id AND blocker.closed_at IS NULL"
                ")",
                tuple(params),
            )
            task_stats["ready"] = ready_rows[0]["cnt"] if ready_rows else 0

            # Blocked = unresolved tasks with unresolved blocking deps
            blocked_rows = db.fetchall(
                "SELECT COUNT(*) as cnt FROM tasks t "
                "WHERE t.closed_at IS NULL "
                f"{tf_aliased} "
                "AND EXISTS ("
                "  SELECT 1 FROM task_dependencies td "
                "  JOIN tasks blocker ON td.depends_on = blocker.id "
                "  WHERE td.task_id = t.id AND blocker.closed_at IS NULL"
                ")",
                tuple(params),
            )
            task_stats["blocked"] = blocked_rows[0]["cnt"] if blocked_rows else 0

            # Closed in last 24h (always relative to now, intersected with window)
            closed_24h_rows = db.fetchall(
                "SELECT COUNT(*) as cnt FROM tasks "
                f"WHERE closed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 days') {time_filter}",
                tuple(params),
            )
            task_stats["closed_24h"] = closed_24h_rows[0]["cnt"] if closed_24h_rows else 0
        except Exception as e:
            logger.warning(f"Failed to get time-filtered task stats: {e}")

        # --- Sessions ---
        session_stats: dict[str, Any] = {
            "active": 0,
            "paused": 0,
            "handoff_ready": 0,
            "total": 0,
            "by_source": {},
        }
        try:
            rows = db.fetchall(
                f"SELECT status, COUNT(*) as cnt FROM sessions "
                f"WHERE 1=1 {time_filter} GROUP BY status",
                tuple(params),
            )
            total = 0
            for row in rows:
                status = row["status"]
                total += row["cnt"]
                if status in session_stats and status != "by_source":
                    session_stats[status] = row["cnt"]
            session_stats["total"] = total

            # By-source breakdown
            by_source: dict[str, dict[str, int]] = {}
            source_rows = db.fetchall(
                "SELECT source, status, COUNT(*) as cnt FROM sessions "
                f"WHERE 1=1 {time_filter} "
                "GROUP BY source, status",
                tuple(params),
            )
            for row in source_rows:
                src = row["source"] or "unknown"
                st = row["status"] or "unknown"
                if src not in by_source:
                    by_source[src] = {}
                by_source[src][st] = row["cnt"]
            session_stats["by_source"] = by_source
        except Exception as e:
            logger.warning(f"Failed to get time-filtered session stats: {e}")

        # --- Memory ---
        memory_stats: dict[str, Any] = {"count": 0, "by_type": {}, "recent_count": 0}
        try:
            rows = db.fetchall(
                f"SELECT memory_type, COUNT(*) as cnt FROM memories "
                f"WHERE 1=1 {time_filter} GROUP BY memory_type",
                tuple(params),
            )
            total = 0
            by_type: dict[str, int] = {}
            for row in rows:
                mtype = row["memory_type"]
                cnt = row["cnt"]
                by_type[mtype] = cnt
                total += cnt
            memory_stats["count"] = total
            memory_stats["by_type"] = by_type

            # Recent = created in last 24h (within the filtered set)
            recent_rows = db.fetchall(
                "SELECT COUNT(*) as cnt FROM memories "
                f"WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 days') {time_filter}",
                tuple(params),
            )
            memory_stats["recent_count"] = recent_rows[0]["cnt"] if recent_rows else 0
        except Exception as e:
            logger.warning(f"Failed to get time-filtered memory stats: {e}")

        # --- Metrics Events ---
        metrics_stats: dict[str, Any] = {
            "tools": {},
            "rules": {},
            "skills": {},
        }
        try:
            # Determine time filter for event queries
            from datetime import UTC, datetime, timedelta

            since: datetime | None = None
            if hours is not None and hours > 0:
                since = datetime.now(UTC) - timedelta(hours=hours)
            elif hours is None and days > 0:
                since = datetime.now(UTC) - timedelta(days=days)

            # Tool call stats
            tool_rows = db.fetchall(
                "SELECT name, COUNT(*) as cnt FROM metrics_events "
                "WHERE event_type = 'tool_call'"
                + (" AND created_at >= ?" if since else "")
                + " GROUP BY name ORDER BY cnt DESC LIMIT 5",
                (since.isoformat(),) if since else (),
            )
            tool_total_row = db.fetchone(
                "SELECT COUNT(*) as cnt FROM metrics_events "
                "WHERE event_type = 'tool_call'" + (" AND created_at >= ?" if since else ""),
                (since.isoformat(),) if since else (),
            )
            metrics_stats["tools"] = {
                "total_calls": tool_total_row["cnt"] if tool_total_row else 0,
                "unique_tools": len(tool_rows),
                "top_5": [{"name": r["name"], "calls": r["cnt"]} for r in tool_rows],
            }

            # Rule eval stats
            rule_rows = db.fetchall(
                "SELECT name, COUNT(*) as cnt, "
                "SUM(CASE WHEN result = 'block' THEN 1 ELSE 0 END) as blocks "
                "FROM metrics_events WHERE event_type = 'rule_eval'"
                + (" AND created_at >= ?" if since else "")
                + " GROUP BY name ORDER BY cnt DESC LIMIT 5",
                (since.isoformat(),) if since else (),
            )
            rule_total_row = db.fetchone(
                "SELECT COUNT(*) as cnt, "
                "SUM(CASE WHEN result = 'block' THEN 1 ELSE 0 END) as blocks "
                "FROM metrics_events WHERE event_type = 'rule_eval'"
                + (" AND created_at >= ?" if since else ""),
                (since.isoformat(),) if since else (),
            )
            metrics_stats["rules"] = {
                "total_evals": (rule_total_row["cnt"] or 0) if rule_total_row else 0,
                "block_count": (rule_total_row["blocks"] or 0) if rule_total_row else 0,
                "top_5": [
                    {"name": r["name"], "evals": r["cnt"], "blocks": r["blocks"]} for r in rule_rows
                ],
            }

            # Skill stats
            skill_rows = db.fetchall(
                "SELECT name, event_type, COUNT(*) as cnt "
                "FROM metrics_events WHERE event_type IN ('skill_search', 'skill_invoke')"
                + (" AND created_at >= ?" if since else "")
                + " GROUP BY name, event_type ORDER BY cnt DESC LIMIT 5",
                (since.isoformat(),) if since else (),
            )
            skill_total = db.fetchone(
                "SELECT "
                "SUM(CASE WHEN event_type = 'skill_search' THEN 1 ELSE 0 END) as searches, "
                "SUM(CASE WHEN event_type = 'skill_invoke' THEN 1 ELSE 0 END) as invocations "
                "FROM metrics_events WHERE event_type IN ('skill_search', 'skill_invoke')"
                + (" AND created_at >= ?" if since else ""),
                (since.isoformat(),) if since else (),
            )
            metrics_stats["skills"] = {
                "total_searches": (skill_total["searches"] or 0) if skill_total else 0,
                "total_invocations": (skill_total["invocations"] or 0) if skill_total else 0,
                "top_5": [
                    {"name": r["name"], "type": r["event_type"], "count": r["cnt"]}
                    for r in skill_rows
                ],
            }
        except Exception as e:
            logger.warning(f"Failed to get metrics event stats: {e}")

        return {
            "days": days,
            "hours": hours,
            "tasks": task_stats,
            "sessions": session_stats,
            "memory": memory_stats,
            "metrics": metrics_stats,
        }
