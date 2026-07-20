"""Health checks and targeted recovery for pg_search BM25 indexes."""

from __future__ import annotations

import time
from contextlib import nullcontext
from typing import Any

import psycopg
from psycopg import sql

BM25_INDEXES = (
    "public.code_symbols_search_bm25",
    "public.code_content_search_bm25",
)
BM25_REPAIR_COMMAND = "gobby postgres repair-code-index"

_REPAIR_LOCK_NAME = "gobby:code-index-bm25-repair"
_CORRUPTION_SQLSTATES = {"XX000", "XX001", "XX002"}


def unavailable_bm25_status(error: str) -> dict[str, Any]:
    """Return the stable degraded payload when PostgreSQL cannot be inspected."""
    return _status_payload(
        [_index_payload(name, state="error", error=error) for name in BM25_INDEXES]
    )


def verify_bm25_indexes(conn: Any) -> dict[str, Any]:
    """Verify every required BM25 index without mutating it."""
    return _status_payload([_verify_index(conn, name) for name in _required_index_names(conn)])


def repair_bm25_indexes(
    dsn: str,
    *,
    timeout_seconds: float = 900,
    connect_timeout: int = 5,
    lock_poll_seconds: float = 0.25,
) -> dict[str, Any]:
    """Verify and selectively rebuild damaged BM25 indexes."""
    try:
        with psycopg.connect(
            dsn,
            connect_timeout=connect_timeout,
            autocommit=True,
        ) as conn:
            conn.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{max(1, int(timeout_seconds * 1000))}ms",),
            )
            if not _acquire_repair_lock(
                conn,
                timeout_seconds=timeout_seconds,
                poll_seconds=lock_poll_seconds,
            ):
                status = verify_bm25_indexes(conn)
                if status["healthy"]:
                    return status
                return _with_status_error(
                    status,
                    f"timed out waiting for BM25 repair lock after {timeout_seconds:g}s",
                )

            try:
                before = verify_bm25_indexes(conn)
                damaged = {item["name"] for item in before["indexes"] if item["state"] == "damaged"}
                rebuild_errors: dict[str, str] = {}
                for name in damaged:
                    try:
                        conn.execute(
                            sql.SQL("REINDEX INDEX {}").format(_qualified_identifier(name))
                        )
                    except psycopg.Error as exc:
                        rebuild_errors[name] = _postgres_error(exc)

                after = verify_bm25_indexes(conn)
                for item in after["indexes"]:
                    name = item["name"]
                    item["repaired"] = name in damaged and item["state"] == "healthy"
                    if name in rebuild_errors:
                        item["state"] = "error"
                        item["error"] = f"REINDEX failed: {rebuild_errors[name]}"
                after["healthy"] = all(item["state"] == "healthy" for item in after["indexes"])
                return after
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (_REPAIR_LOCK_NAME,),
                )
    except psycopg.Error as exc:
        return unavailable_bm25_status(_postgres_error(exc))


def render_bm25_status(status: dict[str, Any]) -> list[str]:
    """Render the code-index portion of PostgreSQL status output."""
    lines = [f"Code index:  {'healthy' if status.get('healthy') else 'degraded'}"]
    for item in status.get("indexes", []):
        suffix = " (repaired)" if item.get("repaired") else ""
        lines.append(f"  {item['name']}: {item['state']}{suffix}")
        if item.get("error"):
            lines.append(f"    Error: {item['error']}")
    if not status.get("healthy"):
        lines.append(f"  Repair: {status.get('repair_command', BM25_REPAIR_COMMAND)}")
    return lines


def _verify_index(conn: Any, name: str) -> dict[str, Any]:
    try:
        transaction = getattr(conn, "transaction", None)
        with transaction() if callable(transaction) else nullcontext():
            row = conn.execute("SELECT to_regclass(%s)::text", (name,)).fetchone()
            if row is None or row[0] is None:
                return _index_payload(
                    name,
                    state="missing",
                    error="required BM25 index is missing; run PostgreSQL setup/migrations",
                )
            rows = conn.execute(
                """
                SELECT check_name, passed, details
                FROM pdb.verify_index(%s::regclass, on_error_stop => true)
                """,
                (name,),
            ).fetchall()
    except psycopg.Error as exc:
        state = "damaged" if exc.sqlstate in _CORRUPTION_SQLSTATES else "error"
        return _index_payload(name, state=state, error=_postgres_error(exc))

    checks = [
        {
            "name": str(check_name),
            "passed": bool(passed),
            "details": None if details is None else str(details),
        }
        for check_name, passed, details in rows
    ]
    if not checks:
        return _index_payload(
            name,
            state="error",
            error="pdb.verify_index returned no verification checks",
        )
    state = "healthy" if all(check["passed"] for check in checks) else "damaged"
    error = None
    if state == "damaged":
        error = "; ".join(
            str(check["details"] or check["name"]) for check in checks if not check["passed"]
        )
    return _index_payload(name, state=state, checks=checks, error=error)


def _acquire_repair_lock(
    conn: Any,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            (_REPAIR_LOCK_NAME,),
        ).fetchone()
        if row and bool(row[0]):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def _qualified_identifier(name: str) -> sql.Identifier:
    schema, index = name.split(".", 1)
    return sql.Identifier(schema, index)


def _required_index_names(conn: Any) -> tuple[str, ...]:
    """Qualify required indexes with the connection's active schema."""
    row = conn.execute("SELECT current_schema()").fetchone()
    schema = str(row[0]) if row and row[0] else "public"
    return tuple(f"{schema}.{name.rsplit('.', 1)[1]}" for name in BM25_INDEXES)


def _index_payload(
    name: str,
    *,
    state: str,
    checks: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "state": state,
        "repaired": False,
        "checks": checks or [],
        "error": error,
    }


def _status_payload(indexes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "healthy": all(item["state"] == "healthy" for item in indexes),
        "repair_command": BM25_REPAIR_COMMAND,
        "indexes": indexes,
    }


def _with_status_error(status: dict[str, Any], error: str) -> dict[str, Any]:
    for item in status["indexes"]:
        if item["state"] != "healthy":
            item["error"] = f"{item['error']}; {error}" if item["error"] else error
    return status


def _postgres_error(exc: psycopg.Error) -> str:
    parts: list[str] = []
    diag = getattr(exc, "diag", None)
    for value in (
        getattr(diag, "message_primary", None),
        getattr(diag, "message_detail", None),
        getattr(diag, "message_hint", None),
    ):
        if value and value not in parts:
            parts.append(str(value))
    fallback = str(exc).strip()
    if fallback and fallback not in parts:
        parts.append(fallback)
    return ": ".join(parts) or type(exc).__name__
