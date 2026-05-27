"""Comment routes for task HTTP endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from gobby.storage.tasks._models import TaskNotFoundError

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.tasks._models import Task


class TaskCommentCreateRequest(BaseModel):
    """Request body for creating a comment."""

    body: str = Field(..., description="Comment body (markdown)")
    author: str = Field(..., description="Author ID (session or user)")
    author_type: str = Field(default="session", description="Author type: session, agent, human")
    parent_comment_id: str | None = Field(
        default=None, description="Parent comment ID for threading"
    )


def register_task_comment_routes(
    router: APIRouter,
    server: HTTPServer,
    *,
    resolve_task: Callable[..., Task],
    broadcast_task: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> None:
    """Register comment routes on the shared tasks router."""

    @router.get("/{task_id}/comments")
    async def list_comments(
        task_id: str,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> Any:
        """List comments for a task, threaded."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id

            total_row = server.task_manager.db.fetchone(
                "SELECT COUNT(*) as total FROM task_comments WHERE task_id = %s",
                (resolved_id,),
            )
            total = total_row["total"] if total_row else 0

            rows = server.task_manager.db.fetchall(
                """SELECT id, task_id, parent_comment_id, author, author_type, body,
                          created_at, updated_at
                   FROM task_comments
                   WHERE task_id = %s
                   ORDER BY created_at ASC
                   LIMIT %s OFFSET %s""",
                (resolved_id, limit, offset),
            )
            comments = [dict(row) for row in rows]
            return {"comments": comments, "count": len(comments), "total": total}
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{task_id}/comments")
    async def create_comment(task_id: str, request_data: TaskCommentCreateRequest) -> Any:
        """Add a comment to a task."""
        try:
            task = resolve_task(task_id)
            resolved_id = task.id

            comment_id = str(uuid.uuid4())
            server.task_manager.db.execute(
                """INSERT INTO task_comments (id, task_id, parent_comment_id, author, author_type, body)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    comment_id,
                    resolved_id,
                    request_data.parent_comment_id,
                    request_data.author,
                    request_data.author_type,
                    request_data.body,
                ),
            )

            row = server.task_manager.db.fetchone(
                "SELECT * FROM task_comments WHERE id = %s", (comment_id,)
            )
            result = dict(row) if row else {"id": comment_id}
            task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
            await broadcast_task("task_comment_added", {**result, "task_ref": task_ref})
            return result
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/{task_id}/comments/{comment_id}")
    async def delete_comment(task_id: str, comment_id: str) -> Any:
        """Delete a comment."""
        try:
            task = resolve_task(task_id)
            server.task_manager.db.execute(
                "DELETE FROM task_comments WHERE id = %s AND task_id = %s",
                (comment_id, task.id),
            )
            return {"deleted": True}
        except TaskNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
