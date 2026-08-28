"""E2E regressions for autonomous build dispatcher handoffs."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call
from uuid import NAMESPACE_URL, uuid5

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.tmux import configure_tmux
from gobby.build.options import BuildOptions
from gobby.build.stage_manifest import resolve_stage_manifest_specs
from gobby.config.app import DaemonConfig
from gobby.config.tmux import TmuxConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._stage_registry_loader import StageRegistryLoader
from gobby.storage.tasks._stage_types import StageManifestSpec
from gobby.system_automation import SystemAutomationLoop
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests._timing import wait_for_async_condition
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.terminal_fixtures import make_live_terminal
from tests.config_runtime_helpers import static_runtime_capture
from tests.storage.tasks._stage_test_helpers import stage_row
from tests.workflows.step_instance_fixtures import make_step_instance

DETECTION_REGISTRY = BundledDetectionRegistry()

pytestmark = pytest.mark.e2e

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    from unittest.mock import patch

    # Patch the cache rather than the function: production modules bind
    # `get_machine_id` by direct import (e.g. storage/agents/_lifecycle.py),
    # so a function patch on the source module misses them, while every
    # import style flows through the cache.
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture(scope="module", autouse=True)
def _configure_tmux_for_module() -> None:
    configure_tmux(TmuxConfig())


class _MiniPipeline:
    enabled = True
    deprecated = False

    def model_dump_json(self) -> str:
        return '{"name":"expand-task"}'


class _MiniPipelineLoader:
    async def load_pipeline(self, name: str, project_id: str | None = None) -> _MiniPipeline | None:
        _ = project_id
        return _MiniPipeline() if name == "expand-task" else None


class _MiniPipelineExecutor:
    def __init__(self) -> None:
        self.loader = _MiniPipelineLoader()

    async def execute(self, **_kwargs: object) -> object:
        return SimpleNamespace(status="completed")


class MiniBuildHarness:
    """Deterministic fake-agent build harness driven by dispatcher wakeups."""

    skipped_stages = ["pr"]

    def __init__(
        self,
        temp_db: Any,
        sample_project: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.db = temp_db
        self.project_id = sample_project["id"]
        self.monkeypatch = monkeypatch
        self.task_manager = LocalTaskManager(temp_db)
        self.session_manager = SessionManager(temp_db)
        self.run_manager = LocalAgentRunManager(temp_db)
        self.mutexes = TaskDispatchMutexManager(temp_db)
        self.spawned: list[dict[str, object]] = []
        self.pipeline_started: asyncio.Event | None = None
        self.pipeline_release: asyncio.Event | None = None
        self.pipeline_execution_ids: list[str] = []
        self.root_id = ""
        self.child_id = ""
        self.stage_registry = create_stage_ops_registry(
            RegistryContext(
                task_manager=self.task_manager,
            )
        )

    async def install(self) -> None:
        from gobby.agents.sync import sync_bundled_agents
        from gobby.skills.sync import sync_bundled_skills

        sync_bundled_skills(self.db)
        sync_bundled_agents(self.db)
        StageRegistryLoader().sync(self.db)

        async def run_inline(func: object, *args: object, **kwargs: object) -> object:
            return cast(Any, func)(*args, **kwargs)

        self.loop = SystemAutomationLoop(
            db=self.db,
            capture_bundle=static_runtime_capture(DaemonConfig()),
            run_db=run_inline,
        )
        self.services = SimpleNamespace(
            database=self.db,
            task_manager=self.task_manager,
            session_manager=self.session_manager,
            agent_runner=SimpleNamespace(),
            worktree_storage=None,
            clone_storage=None,
            git_manager=None,
            clone_manager=None,
            completion_registry=None,
            config=DaemonConfig(),
            code_indexer=None,
            pipeline_executor=_MiniPipelineExecutor(),
            system_automation_loop=self.loop,
            startup_ready=True,
            shutdown_in_progress=False,
        )
        self.loop.set_services(self.services)
        self.loop._running = True
        self.monkeypatch.setattr("gobby.app_context._current_container", self.services)
        self.monkeypatch.setattr(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
            self._fake_spawn_agent_impl,
        )
        self.monkeypatch.setattr(
            "gobby.dispatch.spawn._prepare_plan_adversary_evidence",
            lambda **kwargs: (str(kwargs["prompt"]), None, None),
        )
        self.monkeypatch.setattr(
            "gobby.dispatch.dispatcher._execute_pipeline_background",
            self._fake_pipeline_background,
        )

    def seed(self) -> None:
        self.root_id = self._create_root()
        root = self.task_manager.get_task(self.root_id)
        assert root is not None
        self.task_manager.stage_states.initialize_manifest(
            self.root_id,
            resolve_stage_manifest_specs(
                self.task_manager,
                root,
                "plan_file",
                BuildOptions(isolation="none", skip_stages=self.skipped_stages),
                skip_stages=self.skipped_stages,
            ),
            by_session_id=None,
        )
        self.task_manager.artifacts.set_artifacts_atomic(
            self.root_id,
            plan_file_path="mini-build-plan.md",
            plan_file_hash="sha256-mini-build-plan",
        )

    def launch(self) -> None:
        assert self.loop.schedule_project_dispatch(
            project_id=self.project_id,
            reason="mini_build_launch",
        )

    async def complete_agent(
        self,
        agent_name: str,
        *,
        stage_name: str,
        stage_state: str,
    ) -> None:
        run = await wait_for_async_condition(
            lambda: self._active_run(agent_name, stage_name, stage_state),
            timeout=2.0,
            description=f"{agent_name} {stage_name}:{stage_state} spawn",
        )
        assert run is not None
        await wait_for_async_condition(
            lambda: (
                self.project_id not in self.loop._project_tasks
                and self.project_id not in self.loop._pending_project_dispatches
            ),
            timeout=2.0,
            description=f"{agent_name} dispatch handoff",
        )
        task_id = cast(str, run["task_id"])
        session_id = cast(str, run["child_session_id"])
        schedule_after_completion = False
        with session_context_for_test(session_id):
            if stage_state == "in_progress" and stage_name == "merge":
                result = await self.stage_registry.call(
                    "complete_stage",
                    {"task_id": task_id, "stage_name": stage_name},
                )
            elif stage_state == "in_progress":
                result = await self.stage_registry.call(
                    "submit_for_review",
                    {
                        "task_id": task_id,
                        "stage_name": stage_name,
                        "review_notes": f"{agent_name} ready",
                    },
                )
            elif stage_state == "needs_review":
                if stage_name == "planning":
                    self.task_manager.stage_states.approve_review(
                        task_id,
                        stage_name,
                        by_session_id=session_id,
                        notes=f"{agent_name} approved",
                        dispatch_run_id=cast(str, run["run_id"]),
                    )
                    current_mutex = self.mutexes.get_mutex(task_id)
                    assert current_mutex is not None
                    assert current_mutex.lease_holder is not None
                    assert self.mutexes.release_mutex(task_id, current_mutex.lease_holder)
                    self.task_manager.stage_states.complete_stage(
                        task_id,
                        stage_name,
                        by_session_id="dispatcher",
                    )
                    self.task_manager.release_task_claim(task_id)
                    schedule_after_completion = True
                    result = {"ok": True}
                else:
                    result = await self.stage_registry.call(
                        "approve_review",
                        {
                            "task_id": task_id,
                            "stage_name": stage_name,
                            "approval_notes": f"{agent_name} approved",
                        },
                    )
            else:
                raise AssertionError(f"Unhandled fake stage state: {stage_state}")
        assert "error" not in result
        self.run_manager.complete(cast(str, run["run_id"]), result="ok", tool_calls_count=1)
        run["completed"] = True
        if schedule_after_completion:
            from gobby.build.dispatch_tick import schedule_dispatcher_tick_for_project

            assert schedule_dispatcher_tick_for_project(
                self.db,
                project_id=self.project_id,
                reason="mini_build_planning_approved",
                services=self.services,
            )

    async def assert_clean_final_state(self) -> None:
        await wait_for_async_condition(
            lambda: (
                (root := self.task_manager.get_task(self.root_id)) is not None
                and root.closed_at is not None
            ),
            timeout=3.0,
            description="root task closed",
        )
        await wait_for_async_condition(
            lambda: not self.loop._project_tasks,  # noqa: SLF001 - dispatcher drain assertion
            timeout=2.0,
            description="scheduled dispatcher tasks drained",
        )

        rows = self.db.fetchall(
            """
            SELECT id, closed_at, claimed_by_session_id, is_escalated, escalated_at
              FROM tasks
             WHERE project_id = %s
             ORDER BY created_at
            """,
            (self.project_id,),
        )
        task_ids = [row["id"] for row in rows]
        assert {self.root_id, self.child_id} <= set(task_ids)
        assert all(row["closed_at"] for row in rows)
        assert all(row["claimed_by_session_id"] is None for row in rows)
        assert all(not row["is_escalated"] and row["escalated_at"] is None for row in rows)
        assert self.run_manager.list_active_global(task_ids=task_ids) == []
        assert self.db.fetchone("SELECT COUNT(*) AS count FROM task_dispatch_mutex")["count"] == 0
        assert self.db.fetchone("SELECT COUNT(*) AS count FROM task_dependencies")["count"] == 0

        root_stages = self.task_manager.stage_states.list_for_task(self.root_id)
        assert [stage.stage_name for stage in root_stages] == [
            "planning",
            "expansion",
            "development",
            "epic_qa",
            "merge",
        ]
        assert "pr" not in {stage.stage_name for stage in root_stages}
        assert self.skipped_stages == ["pr"]
        assert all(stage.state == "done" for stage in root_stages)
        assert self.task_manager.artifacts.get_artifacts(self.root_id).plan_file_hash

    def _create_root(self) -> str:
        task = self.task_manager.create_task(
            project_id=self.project_id,
            title="Mini autonomous build",
            description="Tiny graph for dispatcher autonomy regression coverage",
            task_type="epic",
            category="planning",
            validation_criteria="Mini build reaches merge completion",
        )
        self.task_manager.update_task(
            task.id,
            allow_automation=True,
            unattended=True,
            isolation="none",
        )
        return task.id

    def _create_child(self) -> str:
        if self.child_id:
            return self.child_id
        child = self.task_manager.create_task(
            project_id=self.project_id,
            parent_task_id=self.root_id,
            title="Mini implementation leaf",
            task_type="feature",
            category="code",
            validation_criteria="Leaf closes after development review",
            implementation_domain="backend",
        )
        self.task_manager.update_task(
            child.id,
            allow_automation=True,
            unattended=True,
            isolation="none",
        )
        self.task_manager.stage_states.initialize_manifest(
            child.id,
            [StageManifestSpec("development", 0, max_work_attempts=99, max_review_rounds=99)],
            by_session_id=None,
        )
        self.child_id = child.id
        return child.id

    async def _fake_spawn_agent_impl(self, **kwargs: object) -> dict[str, object]:
        agent_name = str(kwargs["agent_lookup_name"])
        task_id = str(kwargs["task_id"])
        child = self.session_manager.register(
            external_id=f"mini-{agent_name}-{len(self.spawned) + 1}",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=self.project_id,
            agent_depth=1,
        )
        run_id = str(uuid5(NAMESPACE_URL, f"gobby:test:mini-build-run:{len(self.spawned) + 1}"))
        run = self.run_manager.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            child_session_id=child.id,
            claimed_session_id=child.id,
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=agent_name,
            task_id=task_id,
            run_id=run_id,
        )
        self.run_manager.start(run.id)
        initial_variables = cast(dict[str, object], kwargs["initial_variables"])
        self.spawned.append(
            {
                "run_id": run.id,
                "task_id": task_id,
                "child_session_id": child.id,
                "agent_name": agent_name,
                "stage_name": initial_variables["stage_name"],
                "stage_state": initial_variables["stage_state"],
                "completed": False,
            }
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    async def _fake_pipeline_background(
        self,
        _executor: object,
        _pipeline: object,
        _inputs: dict[str, object],
        _project_id: str,
        execution_id: str,
        _pipeline_name: str,
        session_id: str | None = None,
    ) -> None:
        del session_id
        self.pipeline_execution_ids.append(execution_id)
        if self.pipeline_started is not None:
            self.pipeline_started.set()
        if self.pipeline_release is not None:
            await asyncio.wait_for(self.pipeline_release.wait(), timeout=2.0)
        self._create_child()
        from gobby.hooks.event_handlers import _dispatch

        terminal_result = _dispatch.on_pipeline_completed(
            {"execution_id": execution_id},
            db=self.db,
            storage=self.mutexes,
        )
        assert terminal_result is not None
        from gobby.build.dispatch_tick import (
            schedule_dispatcher_continuation_for_task,
            schedule_dispatcher_tick_for_project,
        )

        root = self.task_manager.get_task(self.root_id)
        assert root is not None
        if root.allow_automation:
            assert schedule_dispatcher_tick_for_project(
                self.db,
                project_id=self.project_id,
                reason="mini_build_pipeline_completed",
                services=self.services,
            )
        else:
            assert schedule_dispatcher_continuation_for_task(
                self.db,
                task_id=self.root_id,
                reason="mini_build_pipeline_completed",
                services=self.services,
            )
            await self.loop.dispatch_project_once(
                project_id=self.project_id,
                reason="mini_build_pipeline_completed",
                max_ticks=1,
                max_actions=1,
                explicit_task_ids=(self.root_id,),
            )

    def _active_run(
        self,
        agent_name: str,
        stage_name: str,
        stage_state: str,
    ) -> dict[str, object] | None:
        for run in self.spawned:
            if (
                not run["completed"]
                and run["agent_name"] == agent_name
                and run["stage_name"] == stage_name
                and run["stage_state"] == stage_state
            ):
                return run
        return None


@pytest.mark.asyncio
async def test_mini_build_reaches_clean_merge_state_without_manual_dispatch_ticks(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = MiniBuildHarness(temp_db, sample_project, monkeypatch)
    await harness.install()
    harness.seed()
    harness.launch()

    await harness.complete_agent("planner", stage_name="planning", stage_state="in_progress")
    await harness.complete_agent(
        "plan-adversary", stage_name="planning", stage_state="needs_review"
    )
    await harness.complete_agent("expansion-qa", stage_name="expansion", stage_state="needs_review")
    await harness.complete_agent(
        "backend-developer",
        stage_name="development",
        stage_state="in_progress",
    )
    await harness.complete_agent(
        "qa-reviewer", stage_name="development", stage_state="needs_review"
    )
    await harness.complete_agent(
        "epic-reviewer",
        stage_name="epic_qa",
        stage_state="in_progress",
    )
    await harness.complete_agent(
        "epic-reviewer",
        stage_name="epic_qa",
        stage_state="needs_review",
    )
    await harness.complete_agent(
        "merge-orchestrator", stage_name="merge", stage_state="in_progress"
    )

    await harness.assert_clean_final_state()


@pytest.mark.asyncio
async def test_quick_expansion_completion_dispatches_one_reviewer(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = MiniBuildHarness(temp_db, sample_project, monkeypatch)
    harness.pipeline_started = asyncio.Event()
    harness.pipeline_release = asyncio.Event()
    await harness.install()
    harness.seed()
    harness.launch()

    await harness.complete_agent("planner", stage_name="planning", stage_state="in_progress")
    await harness.complete_agent(
        "plan-adversary",
        stage_name="planning",
        stage_state="needs_review",
    )
    await asyncio.wait_for(harness.pipeline_started.wait(), timeout=2.0)
    harness.task_manager.update_task(harness.root_id, allow_automation=False)

    assert harness._active_run("expansion-qa", "expansion", "needs_review") is None
    harness.pipeline_release.set()
    reviewer_run = await wait_for_async_condition(
        lambda: harness._active_run("expansion-qa", "expansion", "needs_review"),
        timeout=10.0,
        description="quick-build expansion reviewer",
    )
    assert reviewer_run is not None

    from gobby.hooks.event_handlers import _dispatch

    execution_id = harness.pipeline_execution_ids[0]
    assert (
        _dispatch.on_pipeline_completed(
            {"execution_id": execution_id},
            db=harness.db,
            storage=harness.mutexes,
        )
        is None
    )
    dispatch_idle = await wait_for_async_condition(
        lambda: (
            harness.project_id not in harness.loop._project_tasks
            and harness.project_id not in harness.loop._pending_project_dispatches
        ),
        timeout=2.0,
        description="quick-build continuation dispatch",
    )
    assert dispatch_idle

    root = harness.task_manager.get_task(harness.root_id)
    reviewer_spawns = [run for run in harness.spawned if run["agent_name"] == "expansion-qa"]
    assert root is not None and root.allow_automation is False
    assert len(reviewer_spawns) == 1


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("GOBBY_RUN_BUILD_CANARY") != "1",
    reason="set GOBBY_RUN_BUILD_CANARY=1 to run the real build canary",
)
def test_real_small_gobby_build_canary(tmp_path: Path) -> None:
    project = tmp_path / "build-canary"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "canary@example.invalid"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Gobby Canary"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    (project / "README.md").write_text("# Build canary\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    plan_file = project / "plan.md"
    plan_file.write_text(
        "Build a tiny deterministic canary by appending one line to README.md.\n",
        encoding="utf-8",
    )

    build_timeout = float(os.environ.get("GOBBY_BUILD_TIMEOUT", "300"))
    result = subprocess.run(
        [
            "uv",
            "run",
            "gobby",
            "build",
            str(plan_file),
            "--yes",
            "--skip-stage",
            "pr",
            "--isolation",
            "none",
        ],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        timeout=build_timeout,
        check=False,
    )

    report = "\n".join(
        [
            "gobby build canary report:",
            f"returncode={result.returncode}",
            f"stdout={result.stdout[-1000:]}",
            f"stderr={result.stderr[-1000:]}",
        ]
    )
    assert result.returncode == 0, report


@pytest.mark.asyncio
async def test_submit_for_review_autonomously_dispatches_reviewer_without_build_resume(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP stage handoff should trigger reviewer dispatch without build resume."""
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    run_manager = LocalAgentRunManager(temp_db)
    worker = session_manager.register(
        external_id="planner-worker",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        agent_depth=1,
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Build plan",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.update_task(
        task.id,
        allow_automation=True,
        isolation="none",
    )
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("planning", 0, max_review_rounds=99)],
        by_session_id=None,
    )
    task_manager.stage_states.start_stage(
        task.id,
        "planning",
        by_session_id=worker.id,
    )
    reviewer_run_id = str(uuid5(NAMESPACE_URL, "gobby-e2e:autonomous-reviewer"))

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = run_manager.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=str(kwargs["task_id"]),
            run_id=reviewer_run_id,
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    async def run_inline(func: object, *args: object, **kwargs: object) -> object:
        return cast(Any, func)(*args, **kwargs)

    automation_loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        run_db=run_inline,
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        worktree_storage=None,
        clone_storage=None,
        git_manager=None,
        clone_manager=None,
        completion_registry=None,
        config=DaemonConfig(),
        code_indexer=None,
        system_automation_loop=automation_loop,
        startup_ready=True,
        shutdown_in_progress=False,
    )
    automation_loop.set_services(services)
    automation_loop._running = True
    automation_loop._event_loop = asyncio.get_running_loop()
    monkeypatch.setattr("gobby.app_context._current_container", services)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(
        "gobby.dispatch.spawn._prepare_plan_adversary_evidence",
        lambda **kwargs: (str(kwargs["prompt"]), None, None),
    )

    registry_task_manager = LocalTaskManager(temp_db)
    registry = create_stage_ops_registry(
        RegistryContext(
            task_manager=registry_task_manager,
        )
    )
    with session_context_for_test(worker.id):
        result = await registry.call(
            "submit_for_review",
            {
                "task_id": task.id,
                "stage_name": "planning",
                "review_notes": "ready for adversary",
            },
        )

    assert result["ok"] is True
    reviewer = await wait_for_async_condition(
        lambda: run_manager.get(reviewer_run_id),
        timeout=2.0,
        description="autonomous reviewer dispatch",
    )
    assert reviewer is not None
    mutex = TaskDispatchMutexManager(temp_db).get_mutex(task.id)
    assert stage_row(temp_db, task.id, "planning")["state"] == "needs_review"
    assert reviewer.agent_name == "plan-adversary"
    assert reviewer.task_id == task.id
    assert mutex is not None
    assert mutex.run_id == reviewer_run_id


@pytest.mark.asyncio
async def test_cancelled_reviewer_wakes_dispatcher_for_replacement_without_build_resume(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an active reviewer should immediately dispatch its replacement."""
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    run_manager = LocalAgentRunManager(temp_db)
    worker = session_manager.register(
        external_id="review-worker",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        agent_depth=1,
    )
    reviewer_session = session_manager.register(
        external_id="stale-reviewer",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        agent_depth=2,
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Reviewable plan",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.update_task(
        task.id,
        allow_automation=True,
        isolation="none",
    )
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("planning", 0, max_review_rounds=99)],
        by_session_id=None,
    )
    task_manager.stage_states.start_stage(task.id, "planning", by_session_id=worker.id)
    task_manager.stage_states.submit_for_review(task.id, "planning", by_session_id=worker.id)
    task_manager.release_task_claim(task.id)
    task_manager.claim_task(task.id, reviewer_session.id)
    stale_run_id = str(uuid5(NAMESPACE_URL, "gobby-e2e:stale-reviewer"))
    replacement_run_id = str(uuid5(NAMESPACE_URL, "gobby-e2e:replacement-reviewer"))

    stale_run = run_manager.create(
        parent_session_id=worker.id,
        child_session_id=reviewer_session.id,
        claimed_session_id=reviewer_session.id,
        provider="codex",
        prompt="review it",
        agent_name="plan-adversary",
        task_id=task.id,
        run_id=stale_run_id,
    )
    run_manager.start(stale_run.id)
    mutex_manager = TaskDispatchMutexManager(temp_db)
    assert mutex_manager.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="review",
        ttl_seconds=300,
        run_id=stale_run.id,
    )

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = run_manager.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=str(kwargs["task_id"]),
            run_id=replacement_run_id,
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        worktree_storage=None,
        clone_storage=None,
        git_manager=None,
        clone_manager=None,
        completion_registry=None,
        config=None,
        code_indexer=None,
    )
    monkeypatch.setattr("gobby.app_context._current_container", services)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(
        "gobby.dispatch.spawn._prepare_plan_adversary_evidence",
        lambda **kwargs: (str(kwargs["prompt"]), None, None),
    )

    monitor = AgentLifecycleMonitor(
        agent_run_manager=run_manager,
        db=temp_db,
        detection_registry=cast(Any, DETECTION_REGISTRY),
        session_manager=session_manager,
        task_manager=task_manager,
    )

    transitioned = await monitor.terminalize_cancelled_run(
        stale_run.id,
        terminal_reason="user_cancelled",
    )

    replacement = await wait_for_async_condition(
        lambda: run_manager.get(replacement_run_id),
        timeout=2.0,
        description="replacement reviewer dispatch",
    )
    task_after_cancel = task_manager.get_task(task.id)
    mutex = mutex_manager.get_mutex(task.id)

    assert transitioned is True
    assert task_after_cancel is not None
    assert task_after_cancel.claimed_by_session_id is None
    assert stage_row(temp_db, task.id, "planning")["state"] == "needs_review"
    assert replacement is not None
    assert replacement.agent_name == "plan-adversary"
    assert replacement.task_id == task.id
    assert mutex is not None
    assert mutex.run_id == replacement_run_id


@pytest.mark.asyncio
async def test_idle_planner_stage_agent_keeps_periodic_enter_and_gets_handoff_reprompt(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    """A stalled planner still gets Enter nudges and later a semantic handoff prompt."""
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    planner = resolve_agent("planner", temp_db, project_id=sample_project["id"])
    assert planner is not None

    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    run_manager = LocalAgentRunManager(temp_db)
    parent = session_manager.register(
        external_id="build-coordinator",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id="planner-worker",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        agent_depth=1,
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Build plan",
        task_type="epic",
        category="planning",
        claimed_by_session_id=child.id,
        validation_criteria="Test task completion is observable.",
    )
    task_manager.update_task(task.id, allow_automation=True)
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("planning", 0, max_review_rounds=99)],
        by_session_id=None,
    )
    task_manager.stage_states.start_stage(task.id, "planning", by_session_id=child.id)
    idle_run_id = str(uuid5(NAMESPACE_URL, "gobby-e2e:idle-planner"))
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        claimed_session_id=child.id,
        provider="codex",
        prompt="Revise the plan",
        agent_name="planner",
        task_id=task.id,
        run_id=idle_run_id,
    )
    run_manager.start(run.id)
    run_manager.update_runtime(run.id, pid=12345)
    _live_run = run_manager.get(run.id)
    assert _live_run is not None
    make_live_terminal(_live_run, db=run_manager.db, session_name="gobby-idle-planner")

    stored_run = run_manager.get(run.id)
    assert stored_run is not None

    AgentStepInstanceManager(temp_db).save(
        make_step_instance(
            child.id,
            agent_name=planner.name,
            current_step="plan",
            variables={"task_claimed": True},
        )
    )
    SessionVariableManager(temp_db).set_variable(child.id, "step_workflow_complete", False)
    temp_db.execute(
        "UPDATE sessions SET updated_at = %s WHERE id = %s",
        ((datetime.now(UTC) - timedelta(seconds=120)).isoformat(), child.id),
    )

    monitor = AgentLifecycleMonitor(
        agent_run_manager=run_manager,
        db=temp_db,
        detection_registry=cast(Any, DETECTION_REGISTRY),
        session_manager=session_manager,
    )
    mock_tmux = AsyncMock()
    mock_tmux.capture_pane.return_value = "❯\n"
    mock_tmux.send_keys.return_value = True
    monitor._tmux = mock_tmux
    monitor._terminal_prompt_monitor._get_tmux = lambda: mock_tmux
    monitor._idle_check_handler._tmux = mock_tmux
    monitor._idle_check_handler._recovery._tmux = mock_tmux
    idle_state = monitor._idle_detector.get_state(stored_run.id)
    idle_state.first_idle_at = time.monotonic() - 120

    assert await monitor.check_periodic_enters() == 1
    mock_tmux.send_keys.assert_called_once_with(
        "gobby-idle-planner",
        PromptDetector.ENTER_KEY,
        literal=False,
    )

    assert await monitor.check_idle_agents() == 0
    mock_tmux.send_keys.assert_called_once_with(
        "gobby-idle-planner",
        PromptDetector.ENTER_KEY,
        literal=False,
    )

    idle_state.first_idle_at = time.monotonic() - 360
    assert await monitor.check_idle_agents() == 1
    sent_prompt = mock_tmux.send_keys.call_args_list[2].args[1]
    assert mock_tmux.send_keys.call_args_list == [
        call("gobby-idle-planner", PromptDetector.ENTER_KEY, literal=False),
        call("gobby-idle-planner", "Escape", literal=False),
        call("gobby-idle-planner", sent_prompt),
        call("gobby-idle-planner", PromptDetector.ENTER_KEY, literal=False),
    ]
    assert all(call_args.args[1] != "Up" for call_args in mock_tmux.send_keys.call_args_list)
    assert "Workflow: planner. Current step: plan." in sent_prompt
    assert (
        "Finish the required Gobby lifecycle MCP transition, then call end_agent_run."
        in sent_prompt
    )
    assert stage_row(temp_db, task.id, "planning")["state"] == "in_progress"
