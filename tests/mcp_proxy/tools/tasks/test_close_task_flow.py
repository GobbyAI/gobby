"""Final close rechecks, atomic storage, cleanup, notifications and memory review."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close as lifecycle
import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import commit_close as _commit_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._notifications import _notification_tasks as notifications
from gobby.storage.tasks import Task, TaskHasOpenChildrenError
from gobby.tasks.close_checklist import CloseChecklist, CloseGateResult
from gobby.workflows.state_manager import SessionVariableManager


def _task(*, criteria: str | None = "Focused tests pass.") -> Task:
    return Task(
        id="00000000-0000-4000-8000-000000000101",
        project_id="00000000-0000-4000-8000-000000000201",
        title="Close checklist leaf",
        category="code",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        updated_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        claimed_by_session_id="00000000-0000-4000-8000-000000000301",
        validation_criteria=criteria,
        review_fail_count=0,
    )


def _ctx(task: Task) -> RegistryContext:
    manager = MagicMock()
    manager.get_task.return_value = task
    manager.list_tasks.return_value = []
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=manager,
            project_manager=MagicMock(),
            session_manager=MagicMock(),
            session_task_manager=MagicMock(),
            session_var_manager=MagicMock(),
        ),
    )


def _ready_evaluation(task: Task) -> CloseEvaluation:
    return CloseEvaluation(
        task.id,
        task=task,
        repo_path="/repo",
        resolved_session_id=task.claimed_by_session_id,
        edit_session_id=task.claimed_by_session_id,
        is_epic=task.task_type == "epic",
        checklist=CloseChecklist(
            tuple(
                CloseGateResult(index, name, "passed", "Passed.")
                for index, name in enumerate(
                    (
                        "children_closed",
                        "linked_commits",
                        "uncommitted_task_edits",
                        "integration_ancestry",
                    ),
                    start=1,
                )
            )
        ),
    )


@pytest.mark.asyncio
async def test_close_evaluation_storage_stays_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.utils.machine_id import require_machine_id

    task = _task()
    ctx = _ctx(task)
    observed: dict[str, list[int]] = {}

    def record(name: str, value: Any) -> Callable[..., Any]:
        def call(*_args: object, **_kwargs: object) -> Any:
            observed.setdefault(name, []).append(threading.get_ident())
            return value

        return call

    manager = cast(MagicMock, ctx.task_manager)
    manager.get_task.side_effect = record("task", task)
    manager.list_tasks.side_effect = record("children", [])
    monkeypatch.setattr(
        ctx, "resolve_session_id", record("resolve_session", "session"), raising=False
    )
    cast(MagicMock, ctx.session_manager).get.side_effect = record(
        "session", SimpleNamespace(id="session", machine_id=require_machine_id())
    )
    cast(MagicMock, ctx.session_var_manager).get_variables.side_effect = record("variables", {})
    monkeypatch.setattr(lifecycle, "resolve_task_id_for_mcp", record("resolve_task", task.id))
    monkeypatch.setattr(lifecycle, "resolve_task_repo_path", record("repo", "/repo"))

    evaluation = await lifecycle._evaluate_close(
        ctx,
        task_id=task.id,
        reason="moot",
        commit_sha=None,
        project_path="/repo",
        closing_session_id="session",
    )

    assert evaluation.ready
    assert set(observed) == {
        "task",
        "children",
        "resolve_session",
        "session",
        "variables",
        "resolve_task",
        "repo",
    }
    loop_thread = threading.get_ident()
    assert {name for name, threads in observed.items() if loop_thread in threads} == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["task", "commits", "dirty", "already_closed"])
async def test_finalization_rechecks_state_before_writing(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    fresh = _ready_evaluation(task)
    if change == "task":
        fresh.task = replace(task, updated_at=task.updated_at + timedelta(seconds=1))
    elif change == "commits":
        fresh.commit_shas = ["new-link"]
    elif change == "dirty":
        fresh.checklist = CloseChecklist(
            (CloseGateResult(3, "uncommitted_task_edits", "failed", "src/a.py is dirty"),)
        )
    else:
        fresh.already_closed = True
    monkeypatch.setattr(lifecycle, "_evaluate_close", AsyncMock(return_value=fresh))
    result = await _commit_close(
        ctx, evaluation, reason="done", changes_summary="Done", commit_sha=None
    )
    manager = cast(MagicMock, ctx.task_manager)
    manager.close_task.assert_not_called()
    manager.close_task_with_commit.assert_not_called()
    if change == "already_closed":
        assert result["closed"] is True
    else:
        assert result["closed"] is False
        assert result["error"] == (
            "uncommitted_task_edits" if change == "dirty" else "close_state_changed"
        )


@pytest.mark.asyncio
async def test_a_child_created_during_the_close_window_asks_for_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    monkeypatch.setattr(lifecycle, "_evaluate_close", AsyncMock(return_value=evaluation))
    manager = cast(MagicMock, ctx.task_manager)
    manager.close_task.side_effect = TaskHasOpenChildrenError(task.id, ["late-child (Late child)"])
    cleanup = MagicMock()
    monkeypatch.setattr(close_finalization, "_cleanup_closed_claim", cleanup)
    result = await _commit_close(
        ctx, evaluation, reason="done", changes_summary="Done", commit_sha=None
    )
    assert result["error"] == "close_state_changed"
    assert "Late child" in result["message"]
    cleanup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("link_commit", [False, True])
async def test_finalization_storage_and_cleanup_stay_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, link_commit: bool
) -> None:
    task = replace(_task(), task_type="epic", seq_num=4242)
    ancestor = replace(task, id="ancestor", title="Ancestor epic", seq_num=4241)
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    evaluation.commit_shas = ["abc1234"]
    evaluation.candidate_commit_sha = "abc1234"
    monkeypatch.setattr(lifecycle, "_evaluate_close", AsyncMock(return_value=evaluation))
    threads: dict[str, list[int]] = {
        name: [] for name in ("get_task", "close", "memory", "archive", "cleanup")
    }

    def recorder(name: str) -> Callable[..., None]:
        def record(*_args: object, **_kwargs: object) -> None:
            threads[name].append(threading.get_ident())

        return record

    def get_task(task_id: str) -> Task:
        threads["get_task"].append(threading.get_ident())
        return ancestor if task_id == ancestor.id else task

    def close(*_args: object, closed_ancestors: list[str], **_kwargs: object) -> None:
        threads["close"].append(threading.get_ident())
        closed_ancestors.append(ancestor.id)

    manager = cast(MagicMock, ctx.task_manager)
    manager.get_task.side_effect = get_task
    manager.close_task.side_effect = close
    manager.close_task_with_commit.side_effect = close
    monkeypatch.setattr(close_finalization, "_queue_close_memory_review", recorder("memory"))
    monkeypatch.setattr(close_finalization, "_cleanup_closed_claim", recorder("cleanup"))
    monkeypatch.setattr("gobby.hooks.event_handlers._plan.on_epic_terminal", recorder("archive"))
    notified: list[tuple[str, int]] = []

    def notify(_db: object, task_id: str, _state: str, **_kwargs: object) -> None:
        notified.append((task_id, threading.get_ident()))

    monkeypatch.setattr(close_finalization, "notify_parent_on_task_state_change", notify)
    result = await _commit_close(
        ctx,
        evaluation,
        reason="done",
        changes_summary="Done",
        commit_sha="abc1234" if link_commit else None,
    )
    assert result["closed"] is True
    assert result["closed_ancestors"] == [
        {"id": "ancestor", "ref": "#4241", "title": "Ancestor epic"}
    ]
    loop_thread = threading.get_ident()
    for name, observed in threads.items():
        assert observed, name
        assert loop_thread not in observed, name
    assert notified == [(ancestor.id, loop_thread), (task.id, loop_thread)]
    writer = manager.close_task_with_commit if link_commit else manager.close_task
    assert writer.call_args.kwargs["expected_updated_at"] == task.updated_at
    if link_commit:
        assert writer.call_args.args == (task.id, "abc1234")
        manager.close_task.assert_not_called()
    else:
        assert writer.call_args.kwargs["closed_commit_sha"] == "abc1234"
        manager.close_task_with_commit.assert_not_called()


def test_closed_task_cleanup_removes_only_its_edit_entry() -> None:
    variables = {
        "claimed_tasks": {"task-1": "#1", "task-2": "#2"},
        "active_task_id": "task-1",
        "task_edited_files": {
            "task-1": ["src/closed.py"],
            "task-2": ["src/remaining.py"],
        },
    }
    session_var_manager = MagicMock()
    session_var_manager.get_variables.return_value = variables
    session_manager = MagicMock()
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            session_task_manager=MagicMock(),
            session_var_manager=session_var_manager,
            session_manager=session_manager,
        ),
    )
    evaluation = CloseEvaluation("task-1")
    evaluation.task_id = "task-1"
    evaluation.resolved_session_id = "session"
    evaluation.edit_session_id = "session"

    close_finalization._cleanup_closed_claim(ctx, evaluation, ["abc123"])

    updates = session_var_manager.merge_variables.call_args.args[1]
    assert updates["task_edited_files"] == {"task-2": ["src/remaining.py"]}
    session_manager.clear_had_edits.assert_not_called()


@pytest.mark.asyncio
async def test_closing_schedules_the_parent_and_ancestor_broadcasts() -> None:
    """The real notifier must reach a loop it can schedule the broadcast on.

    Off the loop, resolve_background_loop returns None, schedule_background_task
    raises RuntimeError, and the notifier swallows it -- so stubbing the
    notifier would hide exactly the defect (#20871). It runs for real here, and
    the loop's own task factory records what it scheduled: the registry is no
    good for this, because a finished notification pops itself out of it.
    """
    task = replace(_task(), task_type="epic", seq_num=4242)
    ancestor = replace(task, id="00000000-0000-4000-8000-000000000414", seq_num=4241)
    ctx = _ctx(task)
    manager = cast(MagicMock, ctx.task_manager)
    manager.get_task.side_effect = lambda task_id, *_a, **_k: (
        ancestor if task_id == ancestor.id else task
    )
    manager.list_tasks.return_value = []

    def record_close(*_args: object, **kwargs: object) -> None:
        closed = kwargs.get("closed_ancestors")
        if isinstance(closed, list):
            closed.append(ancestor.id)

    manager.close_task.side_effect = record_close

    scheduled_tasks: list[asyncio.Task[None]] = []
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()

    def factory(target_loop: Any, coro: Any, **kwargs: Any) -> asyncio.Task[None]:
        created: asyncio.Task[None] = asyncio.Task(coro, loop=target_loop, **kwargs)
        scheduled_tasks.append(created)
        return created

    loop.set_task_factory(cast(Any, factory))
    try:
        with (
            patch.object(
                lifecycle, "_evaluate_close", AsyncMock(return_value=_ready_evaluation(task))
            ),
            patch.object(close_finalization, "_queue_close_memory_review", lambda *a, **k: None),
            patch.object(close_finalization, "_cleanup_closed_claim", lambda *a, **k: None),
            patch("gobby.hooks.event_handlers._plan.on_epic_terminal", lambda *a, **k: None),
        ):
            result = await _commit_close(
                ctx,
                _ready_evaluation(task),
                changes_summary="Implemented the task changes.",
                reason="done",
                commit_sha=None,
            )
    finally:
        loop.set_task_factory(cast(Any, previous_factory))
        for pending in list(notifications.values()):
            pending.cancel()
        await asyncio.gather(*notifications.values(), return_exceptions=True)
        notifications.clear()

    assert result["closed"] is True
    # Python 3.13 applies create_task's name after the task factory returns.
    scheduled = [
        created.get_name()
        for created in scheduled_tasks
        if created.get_name().startswith("gobby-parent-notification-")
    ]
    # Ancestors are described and notified before the task's own broadcast.
    assert scheduled == [
        f"gobby-parent-notification-{ancestor.id}-closed",
        f"gobby-parent-notification-{task.id}-closed",
    ]


def _memory_review_close_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spawned: bool = False,
) -> tuple[RegistryContext, CloseEvaluation, MagicMock]:
    task = _task()
    ctx = _ctx(task)
    evaluation = _ready_evaluation(task)
    evaluation.edit_session_id = task.claimed_by_session_id
    evaluation.commit_shas = ["abc1234"]
    evaluation.candidate_commit_sha = "abc1234"
    root = SimpleNamespace(id="root", parent_session_id=None, agent_run_id=None, agent_depth=0)
    parent = SimpleNamespace(
        id="interactive", parent_session_id="root", agent_run_id=None, agent_depth=0
    )
    child = SimpleNamespace(
        id=task.claimed_by_session_id,
        parent_session_id="interactive" if spawned else None,
        agent_run_id="worker" if spawned else None,
        agent_depth=1 if spawned else 0,
    )
    sessions = {item.id: item for item in (root, parent, child)}
    ctx.session_manager = MagicMock()
    ctx.session_manager.get.side_effect = sessions.get
    manager = cast(MagicMock, ctx.task_manager)

    def close_task(*_args: object, **_kwargs: object) -> Task:
        closed = replace(
            task,
            claimed_by_session_id=None,
            closed_at=datetime(2026, 9, 13, tzinfo=UTC),
            closed_reason="done",
            commits=["abc1234"],
        )
        manager.get_task.return_value = closed
        return closed

    manager.close_task.side_effect = close_task
    manager.close_task_with_commit.side_effect = close_task
    monkeypatch.setattr(lifecycle, "_evaluate_close", AsyncMock(return_value=evaluation))
    monkeypatch.setattr(close_finalization, "notify_parent_on_task_state_change", MagicMock())
    monkeypatch.setattr(close_finalization, "_cleanup_closed_claim", MagicMock())
    queue = MagicMock()
    monkeypatch.setattr(SessionVariableManager, "queue_memory_review", queue)
    return ctx, evaluation, queue


@pytest.mark.asyncio
async def test_commit_close_queues_review_on_interactive_claimer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch)
    result = await _commit_close(
        ctx,
        evaluation,
        reason="done",
        changes_summary="Fixed routing.",
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    queue.assert_called_once_with(
        evaluation.edit_session_id,
        {
            "closure_id": f"{evaluation.task_id}:2026-09-13T00:00:00+00:00",
            "task_id": evaluation.task_id,
            "task_ref": evaluation.task_id,
            "changes_summary": "Fixed routing.",
        },
    )


@pytest.mark.asyncio
async def test_commit_close_queues_review_on_nearest_interactive_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch, spawned=True)
    result = await _commit_close(
        ctx,
        evaluation,
        reason="done",
        changes_summary="Fixed routing.",
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    assert queue.call_count == 1
    assert queue.call_args.args[0] == "interactive"
    assert (
        queue.call_args.args[1]["closure_id"] == f"{evaluation.task_id}:2026-09-13T00:00:00+00:00"
    )


@pytest.mark.asyncio
async def test_memory_review_queue_failure_preserves_committed_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch)
    queue.side_effect = RuntimeError("queue unavailable")
    result = await _commit_close(
        ctx,
        evaluation,
        reason="done",
        changes_summary="Fixed routing.",
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    assert "Failed to queue memory review" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent_run_id", "agent_depth"), [("worker", 0), (None, 1)])
async def test_memory_review_skips_lineage_without_interactive_session(
    monkeypatch: pytest.MonkeyPatch,
    agent_run_id: str | None,
    agent_depth: int,
) -> None:
    ctx, evaluation, queue = _memory_review_close_context(monkeypatch, spawned=True)
    assert evaluation.edit_session_id is not None
    session = ctx.session_manager.get(evaluation.edit_session_id)
    assert session is not None
    session.parent_session_id = None
    session.agent_run_id = agent_run_id
    session.agent_depth = agent_depth
    result = await _commit_close(
        ctx,
        evaluation,
        reason="done",
        changes_summary="Fixed routing.",
        commit_sha="abc1234",
    )
    assert result["closed"] is True
    queue.assert_not_called()
