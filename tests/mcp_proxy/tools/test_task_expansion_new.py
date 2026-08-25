"""Tests for run-oriented task expansion MCP tools."""

import asyncio
import textwrap
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools._background_task_lifecycle import internal_tool_background_loop
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import (
    _background_run_tasks,
    create_expansion_registry,
)
from gobby.mcp_proxy.tools.tasks._expansion_runtime import (
    ScheduledRun,
    cancel_scheduled_expansion_run,
    is_expansion_run_scheduled,
    schedule_expansion_run,
)
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def test_project(project_manager):
    project = project_manager.create(
        name="test-project",
        repo_path="/tmp/test-project",
    )
    return project.id


@pytest.fixture
def test_session(session_manager, test_project):
    session = session_manager.register(
        project_id=test_project,
        source="test",
        external_id="test-external",
        machine_id="21000000-0000-4000-8000-000000000002",
    )
    return session.id


@pytest.fixture
def expansion_registry(task_manager):
    ctx = RegistryContext(
        task_manager=task_manager,
        task_validator_resolver=None,
    )
    return create_expansion_registry(ctx)


@pytest.fixture(autouse=True)
async def clear_background_runs():
    _background_run_tasks.clear()
    yield
    pending = list(_background_run_tasks.values())
    for task in pending:
        if not task.done():
            task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _background_run_tasks.clear()


@pytest.fixture
def parent_task(task_manager, test_project):
    task = task_manager.create_task(
        project_id=test_project,
        title="Parent task for expansion",
        task_type="feature",
        validation_criteria="Test task completion is observable.",
    )
    return task.id


def _compiled_spec() -> dict:
    return {
        "phases": [
            {
                "id": "phase-1",
                "title": "Phase 1: Foundation",
                "summary": "Build the foundation.",
                "test_intent": {
                    "behaviors": ["Writes the new files"],
                    "suggested_test_files": ["tests/test_foundation.py"],
                },
                "task_ids": ["task-1"],
            }
        ],
        "tasks": [
            {
                "id": "task-1",
                "phase_id": "phase-1",
                "title": "Implement the foundation",
                "description": "Create the initial implementation.",
                "category": "code",
                "priority": 2,
                "task_type": "task",
                "validation": "Implementation is present.",
                "affected_files": ["src/foundation.py"],
            }
        ],
        "dependencies": [],
        "execution_groups": [],
    }


def _write_plan_missing_target(tmp_path) -> str:
    path = tmp_path / "plan.md"
    path.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** missing-target

            # Missing Target

            ## P1: Work
            `kind: framing`

            ### 1.1 Work [category: code]
            `kind: deliverable`

            Update implementation.

            **Acceptance:**
            - 1.1.1 - Implementation exists. file: `src/app.py`.
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return str(path)


def _register_active_plan(
    task_manager: LocalTaskManager,
    root_task_id: str,
    *,
    plan_id: str = "registered-plan",
    plan_path: str = ".gobby/plans/registered-plan.md",
) -> str:
    root = task_manager.get_task(root_task_id)
    now = datetime.now(UTC)
    task_manager.db.execute(
        """
        INSERT INTO plans (
            id, project_id, plan_id, plan_path, plan_hash, plan_kind, state,
            root_task_ref, created_at, updated_at, archived_at
        )
        VALUES (%s, %s, %s, %s, NULL, 'implementation', 'active', %s, %s, %s, NULL)
        """,
        (
            str(uuid4()),
            root.project_id,
            plan_id,
            plan_path,
            f"#{root.seq_num}",
            now,
            now,
        ),
    )
    return plan_path


async def _runs_until_cancelled() -> None:
    """A run body that ends only when something cancels it."""
    await asyncio.Event().wait()


async def _drain_queued_callbacks() -> None:
    """Return once every callback queued before this call has run.

    The loop runs its ready queue in order, so a callback queued here lands
    behind the one `call_soon_threadsafe` already posted -- no sleep needed to
    know the scheduling hop is done.
    """
    loop = asyncio.get_running_loop()
    drained = loop.create_future()
    loop.call_soon(drained.set_result, None)
    await drained


class _LoopClosedAtHandoff:
    """A loop that reads open at resolve time and closed at hand-off time.

    `resolve_background_loop`'s `is_closed()` check and the
    `call_soon_threadsafe` hand-off are separated in time; this is the loop
    that closes in between, raising exactly where the real one would.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self.pending_at_handoff: bool | None = None

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, _callback: Any) -> None:
        self.pending_at_handoff = is_expansion_run_scheduled(self._run_id)
        raise RuntimeError("Event loop is closed")


class TestExpansionRuns:
    @pytest.mark.asyncio
    async def test_validate_plan_file_returns_semantic_lint_errors(
        self,
        expansion_registry,
        tmp_path,
    ) -> None:
        result = await expansion_registry.call(
            "validate_plan_file",
            {"plan_file": _write_plan_missing_target(tmp_path)},
        )

        assert result["valid"] is False
        assert any("target-coverage" in error for error in result["errors"])
        assert result["semantic_lint"]["valid"] is False

    @pytest.mark.asyncio
    async def test_an_expansion_tool_body_runs_off_the_event_loop_thread(
        self,
        expansion_registry: Any,
        tmp_path: Path,
    ) -> None:
        """What the synchronous declaration buys these nine tools.

        Every one of them reaches synchronous psycopg, and a tool declared
        `async def` that never awaits runs its whole body on the loop thread
        (#20855). Plan validation is the cheapest of the nine to drive end to
        end, and the registry's dispatch is the same for all of them.
        """
        ran_on: list[int] = []

        def record_thread(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            ran_on.append(threading.get_ident())
            return {"valid": True, "errors": []}

        loop_thread = threading.get_ident()
        with patch(
            "gobby.tasks.expansion_service.ExpansionService.validate_plan_file",
            side_effect=record_thread,
        ):
            await expansion_registry.call(
                "validate_plan_file",
                {"plan_file": _write_plan_missing_target(tmp_path)},
            )

        assert len(ran_on) == 1
        assert ran_on[0] != loop_thread, "an expansion tool body ran on the event loop thread"

    @pytest.mark.asyncio
    async def test_a_run_scheduled_from_a_worker_thread_is_active_before_the_loop_runs(
        self,
    ) -> None:
        """The window the worker-thread hop opens.

        A synchronous tool schedules through `loop.call_soon_threadsafe`, so
        the run's `asyncio.Task` does not exist until the loop next takes a
        callback. Resume and cancel ask whether a run is already going; before
        #20855 they asked `_background_run_tasks`, which is empty for that
        whole window, so a live run read as idle and resume started a second
        execution of it.
        """
        loop = asyncio.get_running_loop()
        run_id = str(uuid4())
        scheduled: list[ScheduledRun] = []

        def schedule_from_worker() -> None:
            with internal_tool_background_loop(loop):
                scheduled.append(schedule_expansion_run(_runs_until_cancelled(), run_id))

        worker = threading.Thread(target=schedule_from_worker)
        worker.start()
        # Joining blocks the loop thread, so the queued callback cannot have
        # run yet -- this is the window, held open deterministically.
        worker.join()

        assert scheduled[0].scheduled is True
        assert run_id not in _background_run_tasks
        assert is_expansion_run_scheduled(run_id) is True

        await _drain_queued_callbacks()

        assert run_id in _background_run_tasks
        assert is_expansion_run_scheduled(run_id) is True

    @pytest.mark.asyncio
    async def test_cancelling_a_run_in_that_window_cancels_the_task_it_produces(
        self,
    ) -> None:
        """Cancel has to reach a run whose task does not exist yet.

        Marking the row cancelled while the queued callback goes on to create
        an uncancelled task leaves the run executing against a row that says
        it stopped.
        """
        loop = asyncio.get_running_loop()
        run_id = str(uuid4())

        def schedule_from_worker() -> None:
            with internal_tool_background_loop(loop):
                schedule_expansion_run(_runs_until_cancelled(), run_id)

        worker = threading.Thread(target=schedule_from_worker)
        worker.start()
        worker.join()

        cancel_scheduled_expansion_run(run_id)
        await _drain_queued_callbacks()

        task = _background_run_tasks[run_id]
        # Bounded: a run that ignored the request never ends on its own, and
        # that has to read as a failure rather than a hung suite.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert task.cancelled() is True
        assert is_expansion_run_scheduled(run_id) is False

    @pytest.mark.asyncio
    async def test_a_failed_loop_handoff_leaves_the_run_schedulable(self) -> None:
        """A hand-off the loop refuses must not wedge the run (#20872).

        The pending entry goes in just before `call_soon_threadsafe`, and only
        the queued callback pops it. A loop that closes between the
        `is_closed()` check and the hand-off raises instead of queueing, so
        nothing ever popped the entry: the run read as active for the life of
        the process and resume short-circuited with "already active".
        """
        run_id = str(uuid4())
        loop_stub = _LoopClosedAtHandoff(run_id)
        outcomes: list[ScheduledRun] = []

        async def _completes_inline() -> str:
            return "ran-inline"

        def schedule_from_worker() -> None:
            with internal_tool_background_loop(cast(asyncio.AbstractEventLoop, loop_stub)):
                outcomes.append(schedule_expansion_run(_completes_inline(), run_id))

        worker = threading.Thread(target=schedule_from_worker)
        worker.start()
        worker.join()

        # The raise landed after the entry went in -- the window that leaked.
        assert loop_stub.pending_at_handoff is True
        # The failed hand-off cleaned up after itself: the run reads as not
        # scheduled, so resume can start it instead of refusing.
        assert is_expansion_run_scheduled(run_id) is False
        # And the caller got the unreachable-loop contract: an inline run.
        assert outcomes[0].scheduled is False
        assert outcomes[0].result == "ran-inline"

    @pytest.mark.asyncio
    async def test_scheduling_an_already_pending_run_keeps_its_recorded_cancel(self) -> None:
        """A second hand-off must not erase what the first one recorded (#20872).

        The guard against double scheduling runs in a worker thread, so two
        schedules can both pass it. The second used to overwrite the pending
        entry, erasing a `cancel_requested` already on it -- and its second
        callback then produced an uncancelled task, so the run executed
        despite the cancel.
        """
        loop = asyncio.get_running_loop()
        run_id = str(uuid4())
        outcomes: list[ScheduledRun] = []

        def schedule_from_worker() -> None:
            with internal_tool_background_loop(loop):
                outcomes.append(schedule_expansion_run(_runs_until_cancelled(), run_id))

        first = threading.Thread(target=schedule_from_worker)
        first.start()
        first.join()

        cancel_scheduled_expansion_run(run_id)

        second = threading.Thread(target=schedule_from_worker)
        second.start()
        second.join()

        # The duplicate reports the run as scheduled -- it is, by the first
        # hand-off -- without queueing a second callback.
        assert outcomes[1].scheduled is True

        await _drain_queued_callbacks()

        task = _background_run_tasks[run_id]
        # The surviving cancel reaches the one task the run produced. Bounded:
        # a run that ignored it never ends on its own, and that has to read as
        # a failure rather than a hung suite.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert task.cancelled() is True
        assert is_expansion_run_scheduled(run_id) is False

    @pytest.mark.asyncio
    async def test_start_expansion_run_creates_run(
        self,
        expansion_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        run_manager = LocalExpansionRunManager(task_manager.db)

        with patch(
            "gobby.mcp_proxy.tools.tasks._expansion_runtime._execute_run_impl",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                result = await expansion_registry.call(
                    "start_expansion_run",
                    {"task_id": parent_task, "auto_apply": False},
                )
            await drain_asyncio_tasks()

        assert result["success"] is True
        assert result["status"] == "running"
        run = run_manager.get(result["run_id"])
        assert run is not None
        assert run.parent_task_id == parent_task
        assert run.input_source == "task"
        assert run.options == {"auto_apply": False}

    @pytest.mark.asyncio
    async def test_start_expansion_run_binds_active_registered_plan(
        self,
        expansion_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        registered_path = _register_active_plan(task_manager, parent_task)
        run_manager = LocalExpansionRunManager(task_manager.db)

        with patch(
            "gobby.mcp_proxy.tools.tasks._expansion_runtime._execute_run_impl",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                result = await expansion_registry.call(
                    "start_expansion_run",
                    {"task_id": parent_task, "plan_file": None, "auto_apply": False},
                )
            await drain_asyncio_tasks()

        run = run_manager.get(result["run_id"])
        assert run is not None
        assert run.parent_task_id == parent_task
        assert run.input_source == "plan"
        assert run.plan_file == registered_path

    @pytest.mark.asyncio
    async def test_start_expansion_run_rejects_conflicting_registered_plan_path(
        self,
        expansion_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        registered_path = _register_active_plan(task_manager, parent_task)

        with session_context_for_test(test_session):
            result = await expansion_registry.call(
                "start_expansion_run",
                {"task_id": parent_task, "plan_file": "docs/plans/conflict.md"},
            )

        assert registered_path in result["error"]
        assert "reset_output=true" in result["error"]
        assert LocalExpansionRunManager(task_manager.db).get_latest_for_task(parent_task) is None

    @pytest.mark.asyncio
    async def test_registered_plan_rebinds_organizational_child_to_root(
        self,
        expansion_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        registered_path = _register_active_plan(task_manager, parent_task)
        root = task_manager.get_task(parent_task)
        child = task_manager.create_task(
            project_id=root.project_id,
            title="Organizational child",
            parent_task_id=root.id,
            task_type="epic",
            validation_criteria="Child organization is observable.",
        )
        run_manager = LocalExpansionRunManager(task_manager.db)

        with patch(
            "gobby.mcp_proxy.tools.tasks._expansion_runtime._execute_run_impl",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                root_result = await expansion_registry.call(
                    "start_expansion_run",
                    {"task_id": root.id, "auto_apply": False},
                )
                child_result = await expansion_registry.call(
                    "start_expansion_run",
                    {"task_id": child.id, "auto_apply": False},
                )
            await drain_asyncio_tasks()

        assert child_result["run_id"] == root_result["run_id"]
        run = run_manager.get(child_result["run_id"])
        assert run is not None
        assert run.parent_task_id == root.id
        assert run.plan_file == registered_path
        assert run_manager.get_latest_for_task(child.id) is None

    @pytest.mark.asyncio
    async def test_start_expansion_run_prefers_nearest_registered_plan(
        self,
        expansion_registry: Any,
        task_manager: LocalTaskManager,
        parent_task: str,
        test_session: str,
    ) -> None:
        ancestor = task_manager.get_task(parent_task)
        _register_active_plan(
            task_manager,
            ancestor.id,
            plan_id="ancestor-strategy-a",
            plan_path=".gobby/plans/ancestor-strategy-a.md",
        )
        _register_active_plan(
            task_manager,
            ancestor.id,
            plan_id="ancestor-strategy-b",
            plan_path=".gobby/plans/ancestor-strategy-b.md",
        )
        child = task_manager.create_task(
            project_id=ancestor.project_id,
            title="Implementation epic",
            parent_task_id=ancestor.id,
            task_type="epic",
            validation_criteria="Implementation epic organization is observable.",
        )
        child_path = _register_active_plan(
            task_manager,
            child.id,
            plan_id="child-implementation",
            plan_path=".gobby/plans/child-implementation.md",
        )
        grandchild = task_manager.create_task(
            project_id=ancestor.project_id,
            title="Organizational grandchild",
            parent_task_id=child.id,
            task_type="epic",
            validation_criteria="Grandchild organization is observable.",
        )
        run_manager = LocalExpansionRunManager(task_manager.db)

        with patch(
            "gobby.mcp_proxy.tools.tasks._expansion_runtime._execute_run_impl",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                child_result = await expansion_registry.call(
                    "start_expansion_run",
                    {"task_id": child.id, "auto_apply": False},
                )
                grandchild_result = await expansion_registry.call(
                    "start_expansion_run",
                    {"task_id": grandchild.id, "auto_apply": False},
                )
            await drain_asyncio_tasks()

        assert child_result["run_id"] == grandchild_result["run_id"]
        run = run_manager.get(child_result["run_id"])
        assert run is not None
        assert run.parent_task_id == child.id
        assert run.plan_file == child_path
        assert run_manager.get_latest_for_task(grandchild.id) is None

    @pytest.mark.asyncio
    async def test_start_expansion_run_rejects_multiple_plans_on_nearest_ancestor(
        self,
        expansion_registry: Any,
        task_manager: LocalTaskManager,
        parent_task: str,
        test_session: str,
    ) -> None:
        _register_active_plan(
            task_manager,
            parent_task,
            plan_id="same-root-a",
            plan_path=".gobby/plans/same-root-a.md",
        )
        _register_active_plan(
            task_manager,
            parent_task,
            plan_id="same-root-b",
            plan_path=".gobby/plans/same-root-b.md",
        )

        with session_context_for_test(test_session):
            result = await expansion_registry.call(
                "start_expansion_run",
                {"task_id": parent_task, "auto_apply": False},
            )

        assert "multiple active registered plans" in result["error"]
        assert LocalExpansionRunManager(task_manager.db).get_latest_for_task(parent_task) is None

    @pytest.mark.asyncio
    async def test_start_expansion_run_explicit_plan_file_selects_ancestor(
        self,
        expansion_registry: Any,
        task_manager: LocalTaskManager,
        parent_task: str,
        test_session: str,
    ) -> None:
        ancestor = task_manager.get_task(parent_task)
        ancestor_path = _register_active_plan(
            task_manager,
            ancestor.id,
            plan_id="ancestor-strategy",
            plan_path=".gobby/plans/ancestor-strategy.md",
        )
        child = task_manager.create_task(
            project_id=ancestor.project_id,
            title="Implementation epic",
            parent_task_id=ancestor.id,
            task_type="epic",
            validation_criteria="Implementation epic organization is observable.",
        )
        _register_active_plan(
            task_manager,
            child.id,
            plan_id="child-implementation",
            plan_path=".gobby/plans/child-implementation.md",
        )

        with patch(
            "gobby.mcp_proxy.tools.tasks._expansion_runtime._execute_run_impl",
            new=AsyncMock(return_value=None),
        ):
            with session_context_for_test(test_session):
                result = await expansion_registry.call(
                    "start_expansion_run",
                    {
                        "task_id": child.id,
                        "plan_file": ancestor_path,
                        "auto_apply": False,
                    },
                )
            await drain_asyncio_tasks()

        run = LocalExpansionRunManager(task_manager.db).get(result["run_id"])
        assert run is not None
        assert run.parent_task_id == ancestor.id
        assert run.plan_file == ancestor_path

    @pytest.mark.asyncio
    async def test_audited_reset_allows_plan_override_on_registered_root(
        self,
        expansion_registry,
        task_manager,
        parent_task,
        test_session,
    ) -> None:
        _register_active_plan(task_manager, parent_task)
        root = task_manager.get_task(parent_task)
        child = task_manager.create_task(
            project_id=root.project_id,
            title="Organizational child",
            parent_task_id=root.id,
            validation_criteria="Child organization is observable.",
        )
        override_path = "docs/plans/audited-override.md"

        with (
            patch(
                "gobby.tasks.expansion_service.ExpansionService.reset_expansion_output",
                autospec=True,
            ) as reset,
            patch(
                "gobby.mcp_proxy.tools.tasks._expansion_runtime._execute_run_impl",
                new=AsyncMock(return_value=None),
            ),
        ):
            with session_context_for_test(test_session):
                result = await expansion_registry.call(
                    "start_expansion_run",
                    {
                        "task_id": child.id,
                        "plan_file": override_path,
                        "reset_output": True,
                        "auto_apply": False,
                    },
                )
            await drain_asyncio_tasks()

        run = LocalExpansionRunManager(task_manager.db).get(result["run_id"])
        assert run is not None
        assert run.parent_task_id == root.id
        assert run.plan_file == override_path
        reset.assert_called_once()
        assert reset.call_args.args[1] == root.id

    @pytest.mark.asyncio
    async def test_get_latest_expansion_run_returns_most_recent(
        self,
        expansion_registry,
        task_manager,
        parent_task,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        first = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        latest = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="plan",
            plan_file="docs/plans/example.md",
        )

        result = await expansion_registry.call(
            "get_latest_expansion_run",
            {"task_id": parent_task},
        )

        assert result["success"] is True
        assert result["run"]["id"] == latest.id
        assert result["run"]["id"] != first.id
        assert result["run"]["plan_file"] == "docs/plans/example.md"

    @pytest.mark.asyncio
    async def test_validate_expansion_run_checks_compiled_and_applied(
        self,
        expansion_registry: Any,
        task_manager: LocalTaskManager,
        parent_task: str,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        run_manager.start(run.id)
        run_manager.save_compiled_spec(run.id, _compiled_spec())
        child = task_manager.create_task(
            project_id=parent.project_id,
            title="Implement the foundation",
            parent_task_id=parent.id,
            category="code",
            validation_criteria="Test task completion is observable.",
        )
        run_manager.mark_applying(run.id)
        run_manager.save_apply_result(
            run.id,
            task_id_map={"task-1": child.id},
            created_task_ids=[child.id],
            completed=True,
        )

        result = await expansion_registry.call(
            "validate_expansion_run",
            {"run_id": run.id},
        )

        assert result["success"] is True
        assert result["compiled"]["valid"] is True
        assert result["applied"]["valid"] is True

    @pytest.mark.asyncio
    async def test_cancel_expansion_run_marks_run_cancelled(
        self,
        expansion_registry: Any,
        task_manager: LocalTaskManager,
        parent_task: str,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )

        result = await expansion_registry.call(
            "cancel_expansion_run",
            {"run_id": run.id},
        )

        assert result["success"] is True
        assert result["run"]["status"] == "cancelled"
        refreshed = run_manager.get(run.id)
        assert refreshed is not None
        assert refreshed.status == "cancelled"

        repeated = await expansion_registry.call(
            "cancel_expansion_run",
            {"run_id": run.id},
        )

        assert repeated["success"] is True
        assert repeated["run"]["status"] == "cancelled"
        after_repeated = run_manager.get(run.id)
        assert after_repeated is not None
        assert after_repeated.error == refreshed.error
        assert after_repeated.completed_at == refreshed.completed_at

    @pytest.mark.parametrize("terminal_status", ["completed", "failed"])
    @pytest.mark.asyncio
    async def test_cancel_expansion_run_preserves_terminal_status(
        self,
        expansion_registry: Any,
        task_manager: LocalTaskManager,
        parent_task: str,
        terminal_status: str,
    ) -> None:
        """The MCP cancellation handler is a no-op for terminal runs."""
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        if terminal_status == "completed":
            run_manager.db.execute(
                "UPDATE expansion_runs SET status = 'applying' WHERE id = %s",
                (run.id,),
            )
            before = run_manager.save_apply_result(
                run.id,
                task_id_map={},
                created_task_ids=[],
            )
        else:
            before = run_manager.fail(run.id, "compile failed")
        assert before is not None

        result = await expansion_registry.call(
            "cancel_expansion_run",
            {"run_id": run.id},
        )

        assert result["success"] is True
        assert result["run"]["status"] == terminal_status
        refreshed = run_manager.get(run.id)
        assert refreshed is not None
        assert refreshed.status == terminal_status
        assert refreshed.error == before.error
        assert refreshed.completed_at == before.completed_at

    @pytest.mark.asyncio
    async def test_resume_persists_compile_and_applies_after_precompile_failure(
        self,
        expansion_registry: Any,
        task_manager: LocalTaskManager,
        parent_task: str,
        test_session: str,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
            options={"auto_apply": True},
        )
        run_manager.fail(run.id, "precompile failure")

        with patch(
            "gobby.tasks.expansion_service.ExpansionService._generate_raw_spec",
            new=AsyncMock(return_value=_compiled_spec()),
        ) as generate_spec:
            with session_context_for_test(test_session):
                result = await expansion_registry.call(
                    "resume_expansion_run",
                    {"run_id": run.id},
                )
            await drain_asyncio_tasks()

        assert result["success"] is True
        assert result["status"] == "running"
        generate_spec.assert_awaited_once()

        resumed = run_manager.get(run.id)
        assert resumed is not None
        assert resumed.status == "completed"
        assert resumed.error is None
        assert resumed.compiled_spec is not None
        assert resumed.task_id_map is not None
        assert set(resumed.task_id_map) == {"task-1"}
        assert resumed.created_task_ids is not None
        assert len(resumed.created_task_ids) == 1

        read_result = await expansion_registry.call(
            "get_expansion_run",
            {"run_id": run.id},
        )
        assert read_result["run"]["compiled_summary"] == {
            "phase_count": 1,
            "task_count": 1,
            "dependency_count": 0,
        }

    def test_failed_compiled_run_cannot_restart(
        self,
        task_manager: LocalTaskManager,
        parent_task: str,
    ) -> None:
        parent = task_manager.get_task(parent_task)
        assert parent is not None
        run_manager = LocalExpansionRunManager(task_manager.db)
        run = run_manager.create(
            parent_task_id=parent.id,
            project_id=parent.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        assert run_manager.start(run.id) is not None
        assert run_manager.save_compiled_spec(run.id, _compiled_spec()) is not None
        failed = run_manager.fail(run.id, "postcompile failure")
        assert failed is not None

        assert run_manager.start(run.id) is None
        refreshed = run_manager.get(run.id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.error == "postcompile failure"
        assert refreshed.completed_at == failed.completed_at
