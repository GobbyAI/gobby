"""Tests for parallel dispatch features (#9576).

Tests for:
- _score_tasks helper (extracted from suggest_next_task)
- suggest_next_tasks (plural) greedy file-conflict selection
- dispatch_batch concurrent agent spawning
- update_observed_files post-hoc annotation
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# _score_tasks helper
# ═══════════════════════════════════════════════════════════════════════


class TestScoreTasks:
    """Tests for the extracted _score_tasks helper."""

    def _make_task(
        self,
        task_id: str,
        priority: int = 2,
        complexity: int | None = None,
        category: str | None = None,
    ) -> MagicMock:
        task = MagicMock()
        task.id = task_id
        task.priority = priority
        task.complexity_score = complexity
        task.category = category
        return task

    def test_priority_dominates_scoring(self) -> None:
        from gobby.mcp_proxy.tools.task_readiness import _score_tasks

        tm = MagicMock()
        tm.list_tasks.return_value = []  # All leaf tasks

        high = self._make_task("high", priority=1)
        low = self._make_task("low", priority=3, complexity=2, category="tests")

        scored = _score_tasks([low, high], tm, prefer_subtasks=True, active_ancestry=[])

        # High priority task should be first despite low having more bonuses
        assert scored[0][0].id == "high"
        assert scored[1][0].id == "low"

    def test_leaf_task_bonus(self) -> None:
        from gobby.mcp_proxy.tools.task_readiness import _score_tasks

        tm = MagicMock()

        parent = self._make_task("parent", priority=2)
        leaf = self._make_task("leaf", priority=2)

        # parent has children, leaf doesn't
        def list_tasks_side(**kwargs):
            if kwargs.get("parent_task_id") == "parent":
                return [MagicMock()]
            return []

        tm.list_tasks.side_effect = list_tasks_side

        scored = _score_tasks([parent, leaf], tm, prefer_subtasks=True, active_ancestry=[])

        # Leaf should score higher (same priority, +25 bonus)
        assert scored[0][0].id == "leaf"
        assert scored[0][2] is True  # is_leaf
        assert scored[1][2] is False  # not leaf

    def test_no_leaf_preference_when_disabled(self) -> None:
        from gobby.mcp_proxy.tools.task_readiness import _score_tasks

        tm = MagicMock()
        tm.list_tasks.return_value = []

        t1 = self._make_task("t1", priority=2)
        t2 = self._make_task("t2", priority=2, complexity=3)

        scored = _score_tasks([t1, t2], tm, prefer_subtasks=False, active_ancestry=[])

        # t2 has complexity bonus, t1 doesn't. Both leaf, but leaf bonus disabled.
        assert scored[0][0].id == "t2"

    def test_returns_sorted_descending(self) -> None:
        from gobby.mcp_proxy.tools.task_readiness import _score_tasks

        tm = MagicMock()
        tm.list_tasks.return_value = []

        tasks = [
            self._make_task("p3", priority=3),
            self._make_task("p0", priority=0),
            self._make_task("p2", priority=2),
        ]

        scored = _score_tasks(tasks, tm, prefer_subtasks=True, active_ancestry=[])

        ids = [s[0].id for s in scored]
        assert ids == ["p0", "p2", "p3"]

    def test_proximity_boost_applied(self) -> None:
        from gobby.mcp_proxy.tools.task_readiness import _score_tasks

        tm = MagicMock()
        tm.list_tasks.return_value = []

        t1 = self._make_task("t1", priority=2)
        t2 = self._make_task("t2", priority=2)

        # t1 has the active task as ancestor (should get proximity boost)
        def mock_get_task(tid):
            task = MagicMock()
            if tid == "t1":
                task.parent_task_id = "active-task"
            else:
                task.parent_task_id = None
            return task

        tm.get_task.side_effect = mock_get_task

        active_ancestry = ["active-task", "root"]
        scored = _score_tasks([t1, t2], tm, prefer_subtasks=True, active_ancestry=active_ancestry)

        # t1 should have proximity boost
        t1_result = next(s for s in scored if s[0].id == "t1")
        t2_result = next(s for s in scored if s[0].id == "t2")
        assert t1_result[3] > 0  # proximity_boost > 0
        assert t2_result[3] == 0  # no proximity


# ═══════════════════════════════════════════════════════════════════════
# suggest_next_tasks (plural)
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_project_context():
    """Fixture providing mock project context for registry creation."""
    with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
        mock_ctx.return_value = {"id": "test-project-id"}
        yield mock_ctx


class TestSuggestNextTasks:
    """Tests for suggest_next_tasks greedy file-conflict selection."""

    def _make_task(self, task_id: str, priority: int = 2, status: str = "open") -> MagicMock:
        task = MagicMock()
        task.id = task_id
        task.priority = priority
        task.status = status
        task.complexity_score = None
        task.category = None
        task.to_brief.return_value = {
            "id": task_id,
            "ref": f"#{task_id}",
            "title": f"Task {task_id}",
        }
        return task

    def _make_af(self, file_path: str) -> MagicMock:
        af = MagicMock()
        af.file_path = file_path
        return af

    def test_non_conflicting_tasks_returned_together(self, mock_project_context) -> None:
        """Tasks touching different files should all be returned."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        tm = MagicMock()
        t1 = self._make_task("t1")
        t2 = self._make_task("t2")
        t3 = self._make_task("t3")

        tm.list_ready_tasks.return_value = [t1, t2, t3]
        tm.list_tasks.return_value = []  # All leaf, no in_progress

        with patch("gobby.mcp_proxy.tools.task_readiness.TaskAffectedFileManager") as MockAFM:
            mock_af = MockAFM.return_value

            def get_files(task_id):
                files_map = {
                    "t1": [self._make_af("src/a.py")],
                    "t2": [self._make_af("src/b.py")],
                    "t3": [self._make_af("src/c.py")],
                }
                return files_map.get(task_id, [])

            mock_af.get_files.side_effect = get_files

            registry = create_readiness_registry(task_manager=tm)
            suggest_tasks = registry.get_tool("suggest_next_tasks")
            result = suggest_tasks(max_count=3)

        assert len(result["suggestions"]) == 3
        assert result["conflicts_avoided"] == 0

    def test_conflicting_tasks_excluded(self, mock_project_context) -> None:
        """Tasks sharing files should not be in the same batch."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        tm = MagicMock()
        t1 = self._make_task("t1", priority=1)  # Higher priority
        t2 = self._make_task("t2", priority=2)  # Lower priority, same file as t1

        tm.list_ready_tasks.return_value = [t1, t2]
        tm.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.task_readiness.TaskAffectedFileManager") as MockAFM:
            mock_af = MockAFM.return_value

            def get_files(task_id):
                # Both touch the same file
                return [self._make_af("src/shared.py")]

            mock_af.get_files.side_effect = get_files

            registry = create_readiness_registry(task_manager=tm)
            suggest_tasks = registry.get_tool("suggest_next_tasks")
            result = suggest_tasks(max_count=3)

        # Only t1 (higher priority) should be selected
        assert len(result["suggestions"]) == 1
        assert result["suggestions"][0]["id"] == "t1"
        assert result["conflicts_avoided"] == 1

    def test_no_file_annotations_treated_as_non_conflicting(self, mock_project_context) -> None:
        """Tasks with no file annotations are allowed (optimistic)."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        tm = MagicMock()
        t1 = self._make_task("t1")
        t2 = self._make_task("t2")  # No file annotations

        tm.list_ready_tasks.return_value = [t1, t2]
        tm.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.task_readiness.TaskAffectedFileManager") as MockAFM:
            mock_af = MockAFM.return_value

            def get_files(task_id):
                if task_id == "t1":
                    return [self._make_af("src/a.py")]
                return []  # t2 has no annotations

            mock_af.get_files.side_effect = get_files

            registry = create_readiness_registry(task_manager=tm)
            suggest_tasks = registry.get_tool("suggest_next_tasks")
            result = suggest_tasks(max_count=3)

        assert len(result["suggestions"]) == 2
        assert result["conflicts_avoided"] == 0

    def test_max_count_respected(self, mock_project_context) -> None:
        """Should not return more than max_count tasks."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        tm = MagicMock()
        tasks = [self._make_task(f"t{i}") for i in range(5)]
        tm.list_ready_tasks.return_value = tasks
        tm.list_tasks.return_value = []

        with patch("gobby.mcp_proxy.tools.task_readiness.TaskAffectedFileManager") as MockAFM:
            mock_af = MockAFM.return_value
            mock_af.get_files.return_value = []  # No annotations

            registry = create_readiness_registry(task_manager=tm)
            suggest_tasks = registry.get_tool("suggest_next_tasks")
            result = suggest_tasks(max_count=2)

        assert len(result["suggestions"]) == 2
        assert result["total_ready"] == 5

    def test_in_progress_files_excluded(self, mock_project_context) -> None:
        """Files from in-progress tasks should block candidate selection."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        tm = MagicMock()
        ready_task = self._make_task("ready", priority=1)
        ip_task = self._make_task("ip", status="in_progress")

        tm.list_ready_tasks.return_value = [ready_task, ip_task]

        def list_tasks_side(**kwargs):
            status = kwargs.get("status")
            if status == "in_progress":
                return [ip_task]
            return []

        tm.list_tasks.side_effect = list_tasks_side

        with patch("gobby.mcp_proxy.tools.task_readiness.TaskAffectedFileManager") as MockAFM:
            mock_af = MockAFM.return_value

            def get_files(task_id):
                if task_id == "ip":
                    return [self._make_af("src/busy.py")]
                if task_id == "ready":
                    return [self._make_af("src/busy.py")]  # Same file as in_progress
                return []

            mock_af.get_files.side_effect = get_files

            registry = create_readiness_registry(task_manager=tm)
            suggest_tasks = registry.get_tool("suggest_next_tasks")
            result = suggest_tasks(max_count=3)

        # ready task conflicts with in-progress file
        assert len(result["suggestions"]) == 0
        assert result["conflicts_avoided"] == 1

    def test_no_ready_tasks_returns_empty(self, mock_project_context) -> None:
        """Should handle no ready tasks gracefully."""
        from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry

        tm = MagicMock()
        tm.list_ready_tasks.return_value = []

        registry = create_readiness_registry(task_manager=tm)
        suggest_tasks = registry.get_tool("suggest_next_tasks")
        result = suggest_tasks()

        assert result["suggestions"] == []
        assert "reason" in result


# ═══════════════════════════════════════════════════════════════════════
# dispatch_batch
# ═══════════════════════════════════════════════════════════════════════


class TestDispatchBatch:
    """Tests for dispatch_batch concurrent agent spawning."""

    @pytest.fixture
    def registry_deps(self):
        """Create mock dependencies for spawn_agent registry."""
        runner = MagicMock()
        runner.spawn = AsyncMock(return_value={"run_id": "run-1", "success": True})
        tm = MagicMock()
        db = MagicMock()
        session_manager = MagicMock()
        return runner, tm, db, session_manager

    @pytest.mark.asyncio
    async def test_dispatches_multiple_agents(self, registry_deps) -> None:
        runner, tm, db, session_manager = registry_deps

        with (
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context") as mock_ctx,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body") as mock_load,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl") as mock_impl,
        ):
            mock_ctx.return_value = {"id": "proj-1"}
            mock_load.return_value = None
            mock_impl.return_value = {"success": True, "run_id": "run-abc"}

            from gobby.mcp_proxy.tools.spawn_agent._factory import create_spawn_agent_registry

            registry = create_spawn_agent_registry(
                runner=runner,
                task_manager=tm,
                db=db,
                session_manager=session_manager,
            )
            dispatch = registry.get_tool("dispatch_batch")

            suggestions = [
                {"id": "task-1", "ref": "#1", "title": "Task 1"},
                {"id": "task-2", "ref": "#2", "title": "Task 2"},
                {"id": "task-3", "ref": "#3", "title": "Task 3"},
            ]

            result = await dispatch(suggestions=suggestions, agent="default")

        assert result["dispatched"] == 3
        assert len(result["results"]) == 3
        assert all(r["success"] for r in result["results"])

    @pytest.mark.asyncio
    async def test_empty_suggestions(self, registry_deps) -> None:
        runner, tm, db, session_manager = registry_deps

        from gobby.mcp_proxy.tools.spawn_agent._factory import create_spawn_agent_registry

        with patch("gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "proj-1"}

            registry = create_spawn_agent_registry(
                runner=runner,
                task_manager=tm,
                db=db,
                session_manager=session_manager,
            )
            dispatch = registry.get_tool("dispatch_batch")
            result = await dispatch(suggestions=[])

        assert result["dispatched"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_partial_failure(self, registry_deps) -> None:
        runner, tm, db, session_manager = registry_deps

        call_count = 0

        async def mock_impl(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("agent spawn failed")
            return {"success": True, "run_id": f"run-{call_count}"}

        with (
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context") as mock_ctx,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body") as mock_load,
            patch("gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl") as mock_impl_patch,
        ):
            mock_ctx.return_value = {"id": "proj-1"}
            mock_load.return_value = None
            mock_impl_patch.side_effect = mock_impl

            from gobby.mcp_proxy.tools.spawn_agent._factory import create_spawn_agent_registry

            registry = create_spawn_agent_registry(
                runner=runner,
                task_manager=tm,
                db=db,
                session_manager=session_manager,
            )
            dispatch = registry.get_tool("dispatch_batch")

            suggestions = [
                {"id": "task-1", "ref": "#1", "title": "Task 1"},
                {"id": "task-2", "ref": "#2", "title": "Task 2"},
                {"id": "task-3", "ref": "#3", "title": "Task 3"},
            ]

            result = await dispatch(suggestions=suggestions, agent="default")

        assert result["dispatched"] == 2
        failed = [r for r in result["results"] if not r["success"]]
        assert len(failed) == 1
        assert "error" in failed[0]


# ═══════════════════════════════════════════════════════════════════════
# update_observed_files
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateObservedFiles:
    """Tests for update_observed_files post-hoc annotation."""

    def _make_ctx(self):
        ctx = MagicMock()
        ctx.task_manager = MagicMock()
        return ctx

    def test_no_commits_returns_empty(self) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_affected_files_registry

        ctx = self._make_ctx()

        task = MagicMock()
        task.id = "task-1"
        task.commits = []
        ctx.task_manager.get_task.return_value = task

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.resolve_task_id_for_mcp",
            return_value="task-1",
        ):
            registry = create_affected_files_registry(ctx)
            update_obs = registry.get_tool("update_observed_files")
            result = update_obs(task_id="task-1")

        assert result["commits_processed"] == 0
        assert result["files_observed"] == 0
        assert result["files"] == []

    def test_commits_produce_file_annotations(self) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_affected_files_registry

        ctx = self._make_ctx()

        task = MagicMock()
        task.id = "task-1"
        task.commits = ["abc123", "def456"]
        ctx.task_manager.get_task.return_value = task

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._affected_files.resolve_task_id_for_mcp",
                return_value="task-1",
            ),
            patch("subprocess.run") as mock_run,
        ):
            # First commit changes 2 files, second changes 1 (with overlap)
            def run_side(cmd, **kwargs):
                sha = cmd[-1]
                result = MagicMock()
                result.returncode = 0
                if sha == "abc123":
                    result.stdout = "src/a.py\nsrc/b.py"
                else:
                    result.stdout = "src/b.py\nsrc/c.py"
                return result

            mock_run.side_effect = run_side

            registry = create_affected_files_registry(ctx)
            update_obs = registry.get_tool("update_observed_files")
            result = update_obs(task_id="task-1")

        assert result["commits_processed"] == 2
        assert result["files_observed"] == 3  # a.py, b.py, c.py (deduped)
        assert sorted(result["files"]) == ["src/a.py", "src/b.py", "src/c.py"]

    def test_invalid_task_id_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_affected_files_registry
        from gobby.storage.tasks import TaskNotFoundError

        ctx = self._make_ctx()

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.resolve_task_id_for_mcp",
            side_effect=TaskNotFoundError("not found"),
        ):
            registry = create_affected_files_registry(ctx)
            update_obs = registry.get_tool("update_observed_files")
            result = update_obs(task_id="nonexistent")

        assert "error" in result

    def test_git_failure_handled_gracefully(self) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_affected_files_registry

        ctx = self._make_ctx()

        task = MagicMock()
        task.id = "task-1"
        ctx.task_manager.get_task.return_value = task
        task.commits = ["abc123"]

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._affected_files.resolve_task_id_for_mcp",
                return_value="task-1",
            ),
            patch("subprocess.run") as mock_run,
        ):
            result_mock = MagicMock()
            result_mock.returncode = 128  # git error
            result_mock.stdout = ""
            mock_run.return_value = result_mock

            registry = create_affected_files_registry(ctx)
            update_obs = registry.get_tool("update_observed_files")
            result = update_obs(task_id="task-1")

        assert result["commits_processed"] == 0
        assert result["files_observed"] == 0
