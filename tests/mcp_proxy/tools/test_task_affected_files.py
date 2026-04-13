"""Tests for task affected files MCP tools."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.task_affected_files import TaskAffectedFile

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_resolve():
    """Mock resolve_task_id_for_mcp to pass through IDs."""
    with patch(
        "gobby.mcp_proxy.tools.tasks._affected_files.resolve_task_id_for_mcp",
        side_effect=lambda tm, tid: tid,
    ):
        yield


@pytest.fixture
def ctx():
    """Create a mock RegistryContext."""
    mock_ctx = MagicMock()
    mock_ctx.task_manager = MagicMock()
    mock_ctx.task_manager.db = MagicMock()
    return mock_ctx


def _make_af(task_id: str, file_path: str, source: str = "manual") -> TaskAffectedFile:
    return TaskAffectedFile(
        id=1,
        task_id=task_id,
        file_path=file_path,
        annotation_source=source,
        created_at="2026-01-01T00:00:00",
    )


class TestSetAffectedFiles:
    def test_set_files_success(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ) as MockMgr:
            mock_mgr = MockMgr.return_value
            mock_mgr.set_files.return_value = [
                _make_af("task-1", "src/a.py"),
                _make_af("task-1", "src/b.py"),
            ]

            registry = create_ops_affected_files_registry(ctx)
            set_files = registry.get_tool("set_affected_files")
            result = set_files(task_id="task-1", files=["src/a.py", "src/b.py"], source="manual")

            assert result["files_set"] == 2
            assert result["files"] == ["src/a.py", "src/b.py"]
            mock_mgr.set_files.assert_called_once_with("task-1", ["src/a.py", "src/b.py"], "manual")

    def test_set_files_invalid_source(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        with patch("gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"):
            registry = create_ops_affected_files_registry(ctx)
            set_files = registry.get_tool("set_affected_files")
            result = set_files(task_id="task-1", files=["src/a.py"], source="invalid")

            assert "error" in result

    def test_set_files_invalid_task(self, ctx) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry
        from gobby.storage.tasks import TaskNotFoundError

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.resolve_task_id_for_mcp",
            side_effect=TaskNotFoundError("not found"),
        ):
            with patch("gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"):
                registry = create_ops_affected_files_registry(ctx)
                set_files = registry.get_tool("set_affected_files")
                result = set_files(task_id="bad-id", files=["src/a.py"])

                assert "error" in result


class TestGetAffectedFiles:
    def test_get_files_success(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ) as MockMgr:
            mock_mgr = MockMgr.return_value
            mock_mgr.get_files.return_value = [
                _make_af("task-1", "src/a.py"),
            ]

            registry = create_ops_affected_files_registry(ctx)
            get_files = registry.get_tool("get_affected_files")
            result = get_files(task_id="task-1")

            assert result["count"] == 1
            assert result["files"][0]["file_path"] == "src/a.py"

    def test_get_files_empty(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ) as MockMgr:
            MockMgr.return_value.get_files.return_value = []

            registry = create_ops_affected_files_registry(ctx)
            get_files = registry.get_tool("get_affected_files")
            result = get_files(task_id="task-1")

            assert result["count"] == 0
            assert result["files"] == []


class TestFindFileOverlaps:
    def test_find_overlaps_success(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ) as MockMgr:
            mock_mgr = MockMgr.return_value
            mock_mgr.find_overlapping_tasks.return_value = {
                ("task-1", "task-2"): ["src/shared.py"],
            }

            registry = create_ops_affected_files_registry(ctx)
            find_overlaps = registry.get_tool("find_file_overlaps")
            result = find_overlaps(task_ids=["task-1", "task-2"])

            assert result["overlapping_pairs"] == 1
            assert result["overlaps"][0]["task_a"] == "task-1"
            assert result["overlaps"][0]["shared_files"] == ["src/shared.py"]

    def test_find_overlaps_none(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ) as MockMgr:
            MockMgr.return_value.find_overlapping_tasks.return_value = {}

            registry = create_ops_affected_files_registry(ctx)
            find_overlaps = registry.get_tool("find_file_overlaps")
            result = find_overlaps(task_ids=["task-1", "task-2"])

            assert result["overlapping_pairs"] == 0
            assert result["overlaps"] == []

    def test_find_overlaps_invalid_task(self, ctx) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry
        from gobby.storage.tasks import TaskNotFoundError

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.resolve_task_id_for_mcp",
            side_effect=TaskNotFoundError("not found"),
        ):
            with patch("gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"):
                registry = create_ops_affected_files_registry(ctx)
                find_overlaps = registry.get_tool("find_file_overlaps")
                result = find_overlaps(task_ids=["bad-id"])

                assert "error" in result


def _make_mock_run(
    compiled_spec: dict | None = None,
    task_id_map: dict[str, str] | None = None,
) -> MagicMock:
    run = MagicMock()
    run.compiled_spec = compiled_spec
    run.task_id_map = task_id_map
    return run


class TestWireAffectedFilesFromRun:
    def test_wire_success(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        spec = {
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Add models",
                    "affected_files": ["src/models.py", "tests/test_models.py"],
                },
                {
                    "id": "task-2",
                    "title": "Add routes",
                    "affected_files": ["src/routes.py"],
                },
            ]
        }
        run = _make_mock_run(spec, {"task-1": "child-1", "task-2": "child-2"})

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ) as MockMgr, patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.LocalExpansionRunManager"
        ) as MockRunMgr:
            mock_mgr = MockMgr.return_value
            mock_mgr.set_files.return_value = []
            MockRunMgr.return_value.get.return_value = run

            registry = create_ops_affected_files_registry(ctx)
            wire_fn = registry.get_tool("wire_affected_files_from_run")
            result = wire_fn(run_id="run-1")

            assert result["wired"] == 2
            assert result["total_tasks"] == 2
            assert result["skipped"] == 0
            assert mock_mgr.set_files.call_count == 2
            mock_mgr.set_files.assert_any_call(
                "child-1", ["src/models.py", "tests/test_models.py"], "expansion"
            )
            mock_mgr.set_files.assert_any_call("child-2", ["src/routes.py"], "expansion")

    def test_wire_skips_tasks_without_files(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        spec = {
            "tasks": [
                {"id": "task-1", "title": "Research", "category": "research"},
                {"id": "task-2", "title": "Add code", "affected_files": ["src/code.py"]},
            ]
        }
        run = _make_mock_run(spec, {"task-2": "child-2"})

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ) as MockMgr, patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.LocalExpansionRunManager"
        ) as MockRunMgr:
            mock_mgr = MockMgr.return_value
            mock_mgr.set_files.return_value = []
            MockRunMgr.return_value.get.return_value = run

            registry = create_ops_affected_files_registry(ctx)
            wire_fn = registry.get_tool("wire_affected_files_from_run")
            result = wire_fn(run_id="run-1")

            assert result["wired"] == 1
            assert result["total_tasks"] == 2
            assert result["skipped"] == 1
            mock_mgr.set_files.assert_called_once_with("child-2", ["src/code.py"], "expansion")

    def test_wire_errors_when_run_missing(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.LocalExpansionRunManager"
        ) as MockRunMgr, patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ):
            MockRunMgr.return_value.get.return_value = None
            registry = create_ops_affected_files_registry(ctx)
            wire_fn = registry.get_tool("wire_affected_files_from_run")
            result = wire_fn(run_id="run-1")

            assert "error" in result
            assert "not found" in result["error"].lower()

    def test_wire_errors_when_compiled_spec_missing(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        run = _make_mock_run(compiled_spec=None, task_id_map={"task-1": "child-1"})

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.LocalExpansionRunManager"
        ) as MockRunMgr, patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ):
            MockRunMgr.return_value.get.return_value = run
            registry = create_ops_affected_files_registry(ctx)
            wire_fn = registry.get_tool("wire_affected_files_from_run")
            result = wire_fn(run_id="run-1")

            assert "error" in result
            assert "no compiled spec" in result["error"].lower()

    def test_wire_errors_when_task_map_missing(self, ctx, mock_resolve) -> None:
        from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry

        run = _make_mock_run(compiled_spec={"tasks": []}, task_id_map=None)

        with patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.LocalExpansionRunManager"
        ) as MockRunMgr, patch(
            "gobby.mcp_proxy.tools.tasks._affected_files.TaskAffectedFileManager"
        ):
            MockRunMgr.return_value.get.return_value = run
            registry = create_ops_affected_files_registry(ctx)
            wire_fn = registry.get_tool("wire_affected_files_from_run")
            result = wire_fn(run_id="run-1")

            assert "error" in result
            assert "no applied task mapping" in result["error"].lower()
