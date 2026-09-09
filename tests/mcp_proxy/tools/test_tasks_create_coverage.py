"""Focused coverage tests for task MCP tools."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import AgentTaskClaimConflictError, TaskNotFoundError
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.isolated_checkout import insert_overlay, install_isolated_checkout_project

pytestmark = pytest.mark.unit


@pytest.fixture
def canonical_task_session(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Session:
    """Register the task-tool session against this test's isolated checkout."""
    isolated = install_isolated_checkout_project(
        temp_db,
        tmp_path / "isolated-checkout",
        monkeypatch=monkeypatch,
    )
    return SessionManager(temp_db).register(
        external_id="task-create-coverage-session",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=isolated.project.id,
        title="Task create coverage session",
    )


@pytest.fixture
def personal_task_session(
    temp_db: HubDatabase,
    canonical_task_session: Session,
) -> Session:
    """Register a personal-project session on the isolated machine."""
    return SessionManager(temp_db).register(
        external_id="personal-task-create-coverage-session",
        machine_id=canonical_task_session.machine_id,
        source="codex",
        project_id=None,
        title="Personal task create coverage session",
    )


class TestCreateTaskTool:
    """Tests for create_task MCP tool."""

    @pytest.fixture(autouse=True)
    def _set_session_context(
        self,
        mock_task_manager: MagicMock,
        temp_db: HubDatabase,
        canonical_task_session: Session,
    ) -> Iterator[None]:
        mock_task_manager.db = temp_db
        with session_context_for_test(canonical_task_session.id):
            yield

    @pytest.mark.asyncio
    async def test_create_task_minimal(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        """Test create_task with minimal arguments."""
        registry = create_task_registry(mock_task_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440001"
        mock_task.seq_num = 42
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440001",
            "title": "New Task",
        }
        # Mock create_task_with_decomposition to return non-decomposed result
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440001", "title": "New Task"},
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await registry.call(
            "create_task",
            {
                "title": "New Task",
                "category": "research",
                "validation_criteria": "Test task completion is observable.",
            },
        )

        assert result == {
            "id": "550e8400-e29b-41d4-a716-446655440001",
            "seq_num": 42,
            "ref": "#42",
        }
        mock_task_manager.create_task_with_decomposition.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_reports_missing_target_paths_without_blocking(
        self,
        mock_task_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Missing target files are advisory and do not prevent task creation."""
        repo_path = tmp_path / "isolated-checkout"
        existing_path = repo_path / "src/gobby/tasks/existing.py"
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_text("def existing() -> None:\n    pass\n")

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440002"
        mock_task.seq_num = 43
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": mock_task.id, "title": "Check targets"},
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await create_task_registry(mock_task_manager).call(
            "create_task",
            {
                "title": "Check targets",
                "category": "code",
                "implementation_domain": "backend",
                "validation_criteria": "The target warning is returned.",
                "description": (
                    "Targets:\n"
                    "- src/gobby/tasks/existing.py::missing_symbol\n"
                    "- src/gobby/tasks/missing.py\n"
                ),
            },
        )

        assert result["targets_not_found"] == ["src/gobby/tasks/missing.py"]
        mock_task_manager.create_task_with_decomposition.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_omits_target_warning_when_all_targets_exist(
        self,
        mock_task_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        repo_path = tmp_path / "isolated-checkout"
        existing_path = repo_path / "src/gobby/tasks/existing.py"
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.touch()

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440003"
        mock_task.seq_num = 44
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": mock_task.id, "title": "Check existing target"},
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await create_task_registry(mock_task_manager).call(
            "create_task",
            {
                "title": "Check existing target",
                "category": "code",
                "implementation_domain": "backend",
                "validation_criteria": "No target warning is returned.",
                "description": "Targets:\n- src/gobby/tasks/existing.py",
            },
        )

        assert "targets_not_found" not in result

    @pytest.mark.asyncio
    async def test_update_reports_only_new_missing_targets(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
        tmp_path: Path,
    ) -> None:
        repo_path = tmp_path / "isolated-checkout"
        existing_path = repo_path / "src/gobby/tasks/existing.py"
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.touch()
        task = SimpleNamespace(
            id="550e8400-e29b-41d4-a716-446655440004",
            seq_num=45,
            project_id=canonical_task_session.project_id,
            task_type="task",
            category="code",
            validation_criteria="The target warning is returned.",
            implementation_domain="backend",
            is_escalated=False,
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task

        result = await create_task_registry(mock_task_manager).call(
            "update_task",
            {
                "task_id": task.id,
                "description": (
                    "Targets:\n"
                    "- src/gobby/tasks/existing.py::new_symbol\n"
                    "- src/gobby/tasks/missing.py"
                ),
                "affected_files": ["src/gobby/tasks/missing.py"],
            },
        )

        assert result["targets_not_found"] == ["src/gobby/tasks/missing.py"]
        mock_task_manager.update_task.assert_called_once()

    async def test_update_checks_targets_in_claimed_session_operation_checkout(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
        temp_db: HubDatabase,
        tmp_path: Path,
    ) -> None:
        overlay = tmp_path / "claimed-overlay"
        target = overlay / "src/gobby/tasks/overlay_only.py"
        target.parent.mkdir(parents=True)
        target.touch()
        insert_overlay(
            temp_db,
            project_id=canonical_task_session.project_id,
            machine_id=canonical_task_session.machine_id,
            path=str(overlay),
            kind="worktree",
        )
        claimed_session = SessionManager(temp_db).register(
            external_id="claimed-operation-session",
            machine_id=canonical_task_session.machine_id,
            source="codex",
            project_id=canonical_task_session.project_id,
            workspace_path=str(overlay),
        )
        task = SimpleNamespace(
            id="550e8400-e29b-41d4-a716-446655440104",
            seq_num=104,
            project_id=canonical_task_session.project_id,
            task_type="task",
            category="code",
            validation_criteria="Target validation uses the claimed checkout.",
            implementation_domain="backend",
            is_escalated=False,
            claimed_by_session_id=claimed_session.id,
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task

        with patch(
            "gobby.mcp_proxy.tools.tasks._crud.require_claim_authority",
            return_value=None,
        ):
            result = await create_task_registry(mock_task_manager).call(
                "update_task",
                {
                    "task_id": task.id,
                    "description": "Targets:\n- src/gobby/tasks/overlay_only.py",
                },
            )

        assert "targets_not_found" not in result
        mock_task_manager.update_task.assert_called_once()

    async def test_update_rejects_unregistered_claimed_checkout_without_primary_fallback(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
        temp_db: HubDatabase,
        tmp_path: Path,
    ) -> None:
        primary_target = tmp_path / "isolated-checkout/src/gobby/tasks/primary_only.py"
        primary_target.parent.mkdir(parents=True)
        primary_target.touch()
        unregistered_overlay = tmp_path / "unregistered-overlay"
        unregistered_overlay.mkdir()
        claimed_session = SessionManager(temp_db).register(
            external_id="unregistered-operation-session",
            machine_id=canonical_task_session.machine_id,
            source="codex",
            project_id=canonical_task_session.project_id,
            workspace_path=str(unregistered_overlay),
        )
        task = SimpleNamespace(
            id="550e8400-e29b-41d4-a716-446655440105",
            seq_num=105,
            project_id=canonical_task_session.project_id,
            task_type="task",
            category="code",
            validation_criteria="Rejected overlays fail with a typed error.",
            implementation_domain="backend",
            is_escalated=False,
            claimed_by_session_id=claimed_session.id,
        )
        mock_task_manager.get_task.return_value = task

        with patch(
            "gobby.mcp_proxy.tools.tasks._crud.require_claim_authority",
            return_value=None,
        ):
            result = await create_task_registry(mock_task_manager).call(
                "update_task",
                {
                    "task_id": task.id,
                    "description": "Targets:\n- src/gobby/tasks/primary_only.py",
                },
            )

        assert result["success"] is False
        assert result["error_type"] == "checkout_unresolved"
        assert "not a registered worktree or clone" in result["error"]
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_title_does_not_resolve_project_checkout(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        task = SimpleNamespace(
            id="550e8400-e29b-41d4-a716-446655440005",
            seq_num=46,
            project_id="550e8400-e29b-41d4-a716-446655440099",
            task_type="task",
            category="research",
            validation_criteria="The title changes.",
            implementation_domain=None,
            is_escalated=False,
        )
        mock_task_manager.get_task.return_value = task
        mock_task_manager.update_task.return_value = task
        registry = create_task_registry(mock_task_manager)

        with patch(
            "gobby.mcp_proxy.tools.tasks._context.RegistryContext.get_project_repo_path"
        ) as get_repo_path:
            result = await registry.call(
                "update_task",
                {"task_id": task.id, "title": "Updated title"},
            )

        assert result == {}
        get_repo_path.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_task_accepts_refactor_category(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Happy-path: create_task(category='refactor') succeeds.

        Expansion produces refactor tasks (expansion_service.py:566). Before this was a
        canonical category, the MCP enum rejected those payloads. This locks in the fix.
        """
        registry = create_task_registry(mock_task_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440099"
        mock_task.seq_num = 99
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440099",
            "title": "Refactor extraction",
        }
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {
                "id": "550e8400-e29b-41d4-a716-446655440099",
                "title": "Refactor extraction",
            },
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await registry.call(
            "create_task",
            {
                "title": "Refactor extraction",
                "category": "refactor",
                "validation_criteria": "Test task completion is observable.",
            },
        )

        assert result["id"] == "550e8400-e29b-41d4-a716-446655440099"
        call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
        assert call_kwargs["category"] == "refactor"

    @pytest.mark.asyncio
    async def test_create_task_accepts_additional_skills_and_affected_files(
        self, mock_task_manager: MagicMock
    ) -> None:
        """create_task forwards skills and stores create-time files as hypotheses."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._crud.TaskAffectedFileManager"
        ) as MockAffectedFiles:
            mock_af_manager = MagicMock()
            MockAffectedFiles.return_value = mock_af_manager

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440088"
            mock_task.seq_num = 88
            mock_task.to_dict.return_value = {"id": mock_task.id, "title": "Metadata task"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": mock_task.id, "title": "Metadata task"},
            }
            mock_task_manager.get_task.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "Metadata task",
                    "category": "code",
                    "implementation_domain": "backend",
                    "validation_criteria": "Update src/gobby/tasks/demo.py",
                    "additional_skills": ["test-driven-development"],
                    "affected_files": ["src/gobby/tasks/demo.py"],
                },
            )

            assert result["id"] == mock_task.id
            call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
            assert call_kwargs["additional_skills"] == ["test-driven-development"]
            mock_af_manager.set_files.assert_called_once_with(
                mock_task.id,
                ["src/gobby/tasks/demo.py"],
                "hypothesis",
            )

    @pytest.mark.asyncio
    async def test_create_task_with_blocks(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        """Test create_task with blocks argument creates dependencies."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440002"
            mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440002"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440002"},
            }
            mock_task_manager.get_task.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "Blocker Task",
                    "category": "research",
                    "blocks": [
                        "550e8400-e29b-41d4-a716-446655440003",
                        "550e8400-e29b-41d4-a716-446655440004",
                    ],
                    "validation_criteria": "Test task completion is observable.",
                },
            )

            assert result["id"] == "550e8400-e29b-41d4-a716-446655440002"
            assert mock_dep_instance.add_dependency.call_count == 2
            mock_dep_instance.add_dependency.assert_any_call(
                "550e8400-e29b-41d4-a716-446655440003",
                "550e8400-e29b-41d4-a716-446655440002",
                "blocks",
            )
            mock_dep_instance.add_dependency.assert_any_call(
                "550e8400-e29b-41d4-a716-446655440004",
                "550e8400-e29b-41d4-a716-446655440002",
                "blocks",
            )

    @pytest.mark.asyncio
    async def test_create_task_with_depends_on(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        """Test create_task with depends_on argument creates dependencies."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440010"
            mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440010"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440010"},
            }
            mock_task_manager.get_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.resolve_task_id_for_mcp") as mock_resolve:
                mock_resolve.side_effect = lambda mgr, ref, pid: ref

                result = await registry.call(
                    "create_task",
                    {
                        "title": "Dependent Task",
                        "category": "research",
                        "depends_on": ["blocker-1", "blocker-2"],
                        "validation_criteria": "Test task completion is observable.",
                    },
                )

                assert result["id"] == "550e8400-e29b-41d4-a716-446655440010"
                assert mock_dep_instance.add_dependency.call_count == 2
                mock_dep_instance.add_dependency.assert_any_call(
                    "550e8400-e29b-41d4-a716-446655440010",
                    "blocker-1",
                    "blocks",
                )
                mock_dep_instance.add_dependency.assert_any_call(
                    "550e8400-e29b-41d4-a716-446655440010",
                    "blocker-2",
                    "blocks",
                )

    @pytest.mark.asyncio
    async def test_create_task_depends_on_with_errors(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        """Test create_task with depends_on handles invalid refs gracefully."""
        with patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager") as MockDepManager:
            mock_dep_instance = MagicMock()
            MockDepManager.return_value = mock_dep_instance

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440011"
            mock_task.seq_num = 1
            mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440011"}
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440011"},
            }
            mock_task_manager.get_task.return_value = mock_task

            with patch("gobby.mcp_proxy.tools.tasks._crud.resolve_task_id_for_mcp") as mock_resolve:
                mock_resolve.side_effect = [
                    "valid-blocker",
                    TaskNotFoundError("not found"),
                ]

                result = await registry.call(
                    "create_task",
                    {
                        "title": "Partial Deps Task",
                        "category": "research",
                        "depends_on": ["valid-ref", "invalid-ref"],
                        "validation_criteria": "Test task completion is observable.",
                    },
                )

                assert result["id"] == "550e8400-e29b-41d4-a716-446655440011"
                assert len(result["dependency_errors"]) == 1
                assert "warning" in result

    @pytest.mark.asyncio
    async def test_create_task_with_labels(
        self,
        mock_task_manager: MagicMock,
    ) -> None:
        """Test create_task with labels argument."""
        registry = create_task_registry(mock_task_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440005"
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440005",
            "labels": ["urgent", "bug"],
        }
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440005", "labels": ["urgent", "bug"]},
        }
        mock_task_manager.get_task.return_value = mock_task

        await registry.call(
            "create_task",
            {
                "title": "Labeled Task",
                "category": "research",
                "labels": ["urgent", "bug"],
                "validation_criteria": "Test task completion is observable.",
            },
        )

        mock_task_manager.create_task_with_decomposition.assert_called_once()
        call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
        assert call_kwargs["labels"] == ["urgent", "bug"]

    @pytest.mark.asyncio
    async def test_create_code_task_requires_validation_criteria(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Test that code tasks are rejected without validation_criteria."""
        registry = create_task_registry(mock_task_manager)

        result = await registry.call(
            "create_task",
            {
                "title": "Implement new feature",
                "category": "code",
            },
        )

        assert "validation_criteria" in result["error"]
        mock_task_manager.create_task_with_decomposition.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_code_task_with_validation_criteria_succeeds(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Test that code tasks succeed when validation_criteria is provided."""
        registry = create_task_registry(mock_task_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440007"
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440007"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440007"},
        }
        mock_task_manager.get_task.return_value = mock_task

        await registry.call(
            "create_task",
            {
                "title": "Implement new feature",
                "category": "code",
                "validation_criteria": "Tests pass and feature works",
                "implementation_domain": "backend",
            },
        )

        mock_task_manager.create_task_with_decomposition.assert_called_once()
        call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
        assert call_kwargs["category"] == "code"
        assert call_kwargs["validation_criteria"] == "Tests pass and feature works"

    @pytest.mark.asyncio
    async def test_create_rejects_malformed_test_reference(
        self, mock_task_manager: MagicMock
    ) -> None:
        registry = create_task_registry(mock_task_manager)

        result = await registry.call(
            "create_task",
            {
                "title": "Reject malformed acceptance reference",
                "category": "code",
                "validation_criteria": "- test: `tests/tasks/test_validation.py`",
                "implementation_domain": "backend",
            },
        )

        assert result["error"] == (
            "tests/tasks/test_validation.py: malformed test reference; expected path::test_symbol"
        )
        mock_task_manager.create_task_with_decomposition.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ignores_prose_after_test_marker(
        self, mock_task_manager: MagicMock
    ) -> None:
        registry = create_task_registry(mock_task_manager)
        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440007"
        mock_task.to_dict.return_value = {"id": mock_task.id}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": mock_task.id},
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await registry.call(
            "create_task",
            {
                "title": "Preserve prose criteria",
                "category": "research",
                "validation_criteria": ("3) Dispatch test: an openapi-template server registers."),
            },
        )

        assert "error" not in result
        mock_task_manager.create_task_with_decomposition.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_validates_only_explicit_test_reference(
        self, mock_task_manager: MagicMock
    ) -> None:
        registry = create_task_registry(mock_task_manager)
        task_id = "550e8400-e29b-41d4-a716-446655440000"
        legacy_task = MagicMock()
        legacy_task.id = task_id
        legacy_task.seq_num = 42
        legacy_task.claimed_by_session_id = None
        legacy_task.task_type = "task"
        legacy_task.category = "research"
        legacy_task.validation_criteria = "test: `tests/tasks/test_validation.py`"
        legacy_task.implementation_domain = None
        legacy_task.is_escalated = False
        mock_task_manager.get_task.return_value = legacy_task
        mock_task_manager.update_task.return_value = MagicMock()

        title_result = await registry.call(
            "update_task",
            {"task_id": task_id, "title": "Updated legacy task"},
        )

        assert title_result == {}
        mock_task_manager.update_task.assert_called_once_with(
            task_id,
            title="Updated legacy task",
        )
        mock_task_manager.update_task.reset_mock()

        criteria_result = await registry.call(
            "update_task",
            {
                "task_id": task_id,
                "validation_criteria": "test: `tests/tasks/test_validation.py`",
            },
        )

        assert "expected path::test_symbol" in criteria_result["error"]
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_non_code_task_without_validation_criteria(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Every non-epic category is rejected without validation_criteria."""
        registry = create_task_registry(mock_task_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440006"
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440006"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440006"},
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await registry.call(
            "create_task",
            {
                "title": "Research auth options",
                "category": "research",
            },
        )

        assert "validation_criteria" in result["error"]
        mock_task_manager.create_task_with_decomposition.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_task_with_all_optional_fields(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
    ) -> None:
        """Test create_task with all optional fields."""
        registry = create_task_registry(mock_task_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440008"
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440008"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440008"},
        }
        mock_task_manager.get_task.return_value = mock_task

        await registry.call(
            "create_task",
            {
                "title": "Full Task",
                "description": "Detailed description",
                "priority": 1,
                "task_type": "feature",
                "parent_task_id": "550e8400-e29b-41d4-a716-446655440009",
                "labels": ["important"],
                "category": "automated",
                "validation_criteria": "Must pass tests",
            },
        )

        call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
        assert call_kwargs["title"] == "Full Task"
        assert call_kwargs["description"] == "Detailed description"
        assert call_kwargs["priority"] == 1
        assert call_kwargs["task_type"] == "feature"
        assert call_kwargs["parent_task_id"] == "550e8400-e29b-41d4-a716-446655440009"
        assert call_kwargs["labels"] == ["important"]
        assert call_kwargs["category"] == "automated"
        assert call_kwargs["validation_criteria"] == "Must pass tests"
        assert call_kwargs["created_in_session_id"] == canonical_task_session.id

    @pytest.mark.asyncio
    async def test_create_task_uses_personal_project(
        self,
        mock_task_manager: MagicMock,
        personal_task_session: Session,
    ) -> None:
        """Test create_task uses the canonical session's personal project."""
        registry = create_task_registry(mock_task_manager)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440010"
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440010"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440010"},
        }
        mock_task_manager.get_task.return_value = mock_task

        with session_context_for_test(personal_task_session.id):
            await registry.call(
                "create_task",
                {
                    "title": "Task",
                    "category": "research",
                    "validation_criteria": "Test task completion is observable.",
                },
            )

        call_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
        assert call_kwargs["project_id"] == PERSONAL_PROJECT_ID

    @pytest.mark.asyncio
    async def test_create_task_with_show_result_on_create(
        self,
        mock_task_manager: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """Test create_task returns full result when show_result_on_create is True."""
        mock_config.get_gobby_tasks_config.return_value.show_result_on_create = True

        registry = create_task_registry(mock_task_manager, startup_config=mock_config)

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440011"
        mock_task.to_dict.return_value = {
            "id": "550e8400-e29b-41d4-a716-446655440011",
            "title": "Full Task",
            "status": "open",
        }
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {
                "id": "550e8400-e29b-41d4-a716-446655440011",
                "title": "Full Task",
                "status": "open",
            },
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await registry.call(
            "create_task",
            {
                "title": "Full Task",
                "category": "research",
                "validation_criteria": "Test task completion is observable.",
            },
        )

        assert result == {
            "id": "550e8400-e29b-41d4-a716-446655440011",
            "title": "Full Task",
            "status": "open",
        }

    @pytest.mark.asyncio
    async def test_create_task_does_not_auto_generate_validation(
        self,
        mock_task_manager: MagicMock,
        mock_task_validator: AsyncMock,
        mock_config: MagicMock,
    ) -> None:
        """Explicit criteria remain authoritative when auto-generation is enabled."""
        mock_config.get_gobby_tasks_config.return_value.validation.auto_generate_on_create = True

        registry = create_task_registry(
            mock_task_manager,
            task_validator_resolver=lambda: mock_task_validator,
            startup_config=mock_config,
        )

        mock_task = MagicMock()
        mock_task.id = "550e8400-e29b-41d4-a716-446655440012"
        mock_task.task_type = "task"  # Not epic
        mock_task.to_dict.return_value = {"id": "550e8400-e29b-41d4-a716-446655440012"}
        mock_task_manager.create_task_with_decomposition.return_value = {
            "task": {"id": "550e8400-e29b-41d4-a716-446655440012"},
        }
        mock_task_manager.get_task.return_value = mock_task

        result = await registry.call(
            "create_task",
            {
                "title": "Task",
                "category": "research",
                "validation_criteria": "Test task completion is observable.",
            },
        )

        mock_task_manager.update_task.assert_not_called()
        create_kwargs = mock_task_manager.create_task_with_decomposition.call_args.kwargs
        assert create_kwargs["validation_criteria"] == "Test task completion is observable."
        assert "validation_generated" not in result

    @pytest.mark.asyncio
    async def test_create_task_default_no_claim(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
    ) -> None:
        """Test create_task without claim parameter does NOT auto-claim."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440020"
            mock_task.seq_num = 100
            mock_task.status = "open"
            mock_task.claimed_by_session_id = None
            mock_task.to_dict.return_value = {
                "id": "550e8400-e29b-41d4-a716-446655440020",
                "status": "open",
                "claimed_by_session_id": None,
            }
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": "550e8400-e29b-41d4-a716-446655440020"},
            }
            mock_task_manager.get_task.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "New Task",
                    "category": "research",
                    "validation_criteria": "Test task completion is observable.",
                },
            )

            assert result["id"] == "550e8400-e29b-41d4-a716-446655440020"
            mock_task_manager.update_task.assert_not_called()
            mock_st_instance.link_task.assert_called_once_with(
                canonical_task_session.id,
                "550e8400-e29b-41d4-a716-446655440020",
                "created",
            )

    @pytest.mark.asyncio
    async def test_create_task_with_claim_true(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
    ) -> None:
        """Test create_task with claim=True auto-claims the task."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440021"
            mock_task.seq_num = 101
            mock_task.status = "in_progress"
            mock_task.claimed_by_session_id = "test-session"
            mock_task.to_dict.return_value = {
                "id": "550e8400-e29b-41d4-a716-446655440021",
                "status": "in_progress",
                "claimed_by_session_id": "test-session",
            }
            mock_task_manager.create_task_for_agent.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "New Task",
                    "category": "research",
                    "claim": True,
                    "validation_criteria": "Test task completion is observable.",
                },
            )

            assert result["id"] == "550e8400-e29b-41d4-a716-446655440021"
            claim_kwargs = mock_task_manager.create_task_for_agent.call_args.kwargs
            assert claim_kwargs["session_id"] == canonical_task_session.id
            assert claim_kwargs["title"] == "New Task"
            mock_task_manager.claim_task.assert_not_called()
            assert mock_st_instance.link_task.call_count == 2
            mock_st_instance.link_task.assert_any_call(
                canonical_task_session.id,
                "550e8400-e29b-41d4-a716-446655440021",
                "created",
            )
            mock_st_instance.link_task.assert_any_call(
                canonical_task_session.id,
                "550e8400-e29b-41d4-a716-446655440021",
                "claimed",
            )

    @pytest.mark.asyncio
    async def test_create_and_claim_refuses_second_task_without_creating_it(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
    ) -> None:
        """A claim-capacity failure leaves task storage and session links untouched."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_session_tasks = MagicMock()
            MockSessionTaskManager.return_value = mock_session_tasks
            mock_task_manager.create_task_for_agent.side_effect = AgentTaskClaimConflictError(
                "550e8400-e29b-41d4-a716-446655440040",
                "#40",
            )
            registry = create_task_registry(mock_task_manager)

            result = await registry.call(
                "create_task",
                {
                    "title": "Second claimed task",
                    "category": "research",
                    "claim": True,
                    "validation_criteria": "The task is never created.",
                },
            )

            assert result == {
                "success": False,
                "status": "error",
                "error": "Session already owns open claimed task #40",
                "error_code": "TASK_CLAIM_CONFLICT",
                "claimed_task_id": "550e8400-e29b-41d4-a716-446655440040",
                "claimed_task_ref": "#40",
                "message": (
                    "Task was not created. Finish and close task #40 before creating and "
                    "claiming another task."
                ),
            }
            mock_task_manager.create_task_with_decomposition.assert_not_called()
            mock_task_manager.get_task.assert_not_called()
            mock_session_tasks.link_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_task_with_claim_sets_task_claimed_via_session_variables(
        self,
        mock_task_manager: MagicMock,
        canonical_task_session: Session,
    ) -> None:
        """create_task(claim=True) must set task_claimed via session_var_manager.

        Regression test for #8642.
        """
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {}
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440021"
            mock_task.seq_num = 101
            mock_task.status = "in_progress"
            mock_task.claimed_by_session_id = "test-session"
            mock_task_manager.create_task_for_agent.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "New Task",
                    "category": "research",
                    "claim": True,
                    "validation_criteria": "Test task completion is observable.",
                },
            )

            assert result["id"] == "550e8400-e29b-41d4-a716-446655440021"
            mock_sv_manager.merge_variables.assert_called_once()
            call_args = mock_sv_manager.merge_variables.call_args
            assert call_args[0][0] == canonical_task_session.id
            merged_vars = call_args[0][1]
            assert merged_vars["task_claimed"] is True
            assert mock_task.id in merged_vars["claimed_tasks"]

    @pytest.mark.asyncio
    async def test_create_task_with_claim_sets_extra_skills(
        self, mock_task_manager: MagicMock
    ) -> None:
        """create_task(claim=True) persists ordered claimed-task extras."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as MockSVManager,
        ):
            MockSessionTaskManager.return_value = MagicMock()

            mock_sv_manager = MagicMock()
            mock_sv_manager.get_variables.return_value = {}
            MockSVManager.return_value = mock_sv_manager

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440021"
            mock_task.seq_num = 101
            mock_task.title = "Update src/gobby/tasks/demo.py"
            mock_task.description = None
            mock_task.category = "code"
            mock_task.labels = []
            mock_task.validation_criteria = "Update src/gobby/tasks/demo.py"
            mock_task.additional_skills = ["context7"]
            mock_task.to_dict.return_value = {"id": mock_task.id, "title": mock_task.title}
            mock_task_manager.create_task_for_agent.return_value = mock_task
            mock_task_manager.get_task.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": mock_task.title,
                    "category": "code",
                    "implementation_domain": "backend",
                    "validation_criteria": mock_task.validation_criteria,
                    "additional_skills": ["context7"],
                    "claim": True,
                },
            )

            assert result["id"] == mock_task.id
            merged_vars = mock_sv_manager.merge_variables.call_args[0][1]
            assert merged_vars["claimed_task_extra_skills"] == ["context7"]


# =============================================================================
# Cross-Project Claim Blocking Tests (create_task)
# =============================================================================


class TestCreateTaskCrossProjectClaimBlocking:
    """Tests for cross-project claim blocking in create_task."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self) -> Iterator[None]:
        with session_context_for_test("test-session"):
            yield

    @pytest.mark.asyncio
    async def test_create_task_claim_skipped_when_cross_project(
        self, mock_task_manager: MagicMock
    ) -> None:
        """create_task(claim=True) creates the task but skips claiming when cross-project."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
            patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as MockProjManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            # Session is in proj-2, but task will be created in proj-1
            mock_session_manager.get.return_value = MagicMock(
                project_id="11111111-1111-4111-8111-111111110002"
            )
            MockSessionManager.return_value = mock_session_manager

            # Mock project resolution so explicit project="11111111-1111-4111-8111-111111110001" resolves
            mock_proj_instance = MagicMock()
            mock_proj_instance.resolve_ref.return_value = MagicMock(
                id="11111111-1111-4111-8111-111111110001"
            )
            MockProjManager.return_value = mock_proj_instance

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440099"
            mock_task.seq_num = 500
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": mock_task.id},
            }
            mock_task_manager.get_task.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "Cross-project task",
                    "category": "research",
                    "claim": True,
                    "project": "11111111-1111-4111-8111-111111110001",
                    "validation_criteria": "Test task completion is observable.",
                },
            )

            # Task should be created
            assert result["id"] == mock_task.id
            # Warning about skipped claim
            assert "warning" in result
            assert "different project" in result["warning"].lower()
            # update_task should NOT have been called (claim skipped)
            mock_task_manager.update_task.assert_not_called()
            # Session link for "created" should still exist, but NOT "claimed"
            mock_st_instance.link_task.assert_called_once_with(
                "test-session", mock_task.id, "created"
            )

    @pytest.mark.asyncio
    async def test_create_task_claim_allowed_when_same_project(
        self, mock_task_manager: MagicMock
    ) -> None:
        """create_task(claim=True) claims normally when projects match."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = MagicMock(
                project_id="11111111-1111-4111-8111-111111110001",
                status="awaiting_handoff",
            )
            mock_session_manager.update_session_status.return_value = True
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager)

            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440099"
            mock_task.seq_num = 500
            mock_task.status = "in_progress"
            mock_task.claimed_by_session_id = "test-session"

            def create_after_activity(*args: Any, **kwargs: Any) -> MagicMock:
                mock_session_manager.update_session_status.assert_called_once_with(
                    "test-session",
                    "active",
                    activity_confirmed=True,
                )
                return mock_task

            mock_task_manager.create_task_for_agent.side_effect = create_after_activity

            with patch("gobby.mcp_proxy.tools.tasks._context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": "11111111-1111-4111-8111-111111110001"}

                result = await registry.call(
                    "create_task",
                    {
                        "title": "Same-project task",
                        "category": "research",
                        "claim": True,
                        "validation_criteria": "Test task completion is observable.",
                    },
                )

                # Task should be created and claimed
                assert result["id"] == mock_task.id
                assert "warning" not in result
                call_kwargs = mock_task_manager.create_task_for_agent.call_args.kwargs
                assert call_kwargs["session_id"] == "test-session"
                mock_task_manager.claim_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_task_claim_skipped_when_session_cannot_reactivate(
        self, mock_task_manager: MagicMock
    ) -> None:
        """Creation succeeds without a claim when confirmed activity cannot be stored."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            MockSessionTaskManager.return_value = MagicMock()
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "test-session"
            mock_session_manager.get.return_value = MagicMock(
                project_id="11111111-1111-4111-8111-111111110001",
                status="awaiting_handoff",
            )
            mock_session_manager.update_session_status.return_value = False
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager)
            mock_task = MagicMock()
            mock_task.id = "550e8400-e29b-41d4-a716-446655440100"
            mock_task.seq_num = 501
            mock_task_manager.create_task_with_decomposition.return_value = {
                "task": {"id": mock_task.id},
            }
            mock_task_manager.get_task.return_value = mock_task

            result = await registry.call(
                "create_task",
                {
                    "title": "Unclaimed task",
                    "category": "research",
                    "claim": True,
                    "validation_criteria": "Test task completion is observable.",
                },
            )

            assert result["id"] == mock_task.id
            assert "could not be marked active" in result["warning"]
            mock_task_manager.claim_task.assert_not_called()


# =============================================================================
# get_task Tool Tests
# =============================================================================
