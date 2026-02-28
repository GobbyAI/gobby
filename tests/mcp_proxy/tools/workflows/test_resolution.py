"""Tests for workflow resolution utilities.

Exercises the real resolve_session_id and resolve_session_task_value
functions with all code paths:
- resolve_session_id with/without project context
- resolve_session_task_value for non-string, UUID, #N, N, no session,
  no project, task not found, and unexpected errors
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.workflows._resolution import (
    resolve_session_id,
    resolve_session_task_value,
)
from gobby.storage.tasks._models import TaskNotFoundError

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase
    from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_session_manager(temp_db: LocalDatabase) -> LocalSessionManager:
    """Create a real LocalSessionManager backed by a temp database."""
    from gobby.storage.sessions import LocalSessionManager

    return LocalSessionManager(temp_db)


@pytest.fixture
def sample_session(
    real_session_manager: LocalSessionManager,
    sample_project: dict,
) -> MagicMock:
    """Create a real session in the database and return it."""
    session = real_session_manager.register(
        external_id="ext-001",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
        title="Test session",
    )
    return session


# ---------------------------------------------------------------------------
# resolve_session_id
# ---------------------------------------------------------------------------


class TestResolveSessionId:
    """Tests for resolve_session_id function."""

    @patch("gobby.mcp_proxy.tools.workflows._resolution.get_project_context")
    def test_resolves_with_project_id_from_context(
        self,
        mock_ctx: MagicMock,
        real_session_manager: LocalSessionManager,
        sample_session: MagicMock,
    ) -> None:
        """When project context has an ID, it's passed to resolve_session_reference."""
        mock_ctx.return_value = {"id": sample_session.project_id}

        result = resolve_session_id(real_session_manager, sample_session.id)

        assert result == sample_session.id

    @patch("gobby.mcp_proxy.tools.workflows._resolution.get_project_context")
    def test_resolves_with_no_project_context(
        self,
        mock_ctx: MagicMock,
        real_session_manager: LocalSessionManager,
        sample_session: MagicMock,
    ) -> None:
        """When no project context, None is passed as project_id."""
        mock_ctx.return_value = None

        # UUID resolution should work without project_id
        result = resolve_session_id(real_session_manager, sample_session.id)
        assert result == sample_session.id

    @patch("gobby.mcp_proxy.tools.workflows._resolution.get_project_context")
    def test_resolves_with_context_missing_id_key(
        self,
        mock_ctx: MagicMock,
        real_session_manager: LocalSessionManager,
        sample_session: MagicMock,
    ) -> None:
        """When context dict exists but has no 'id' key."""
        mock_ctx.return_value = {"name": "some-project"}

        result = resolve_session_id(real_session_manager, sample_session.id)
        assert result == sample_session.id

    @patch("gobby.mcp_proxy.tools.workflows._resolution.get_project_context")
    def test_delegates_to_session_manager(self, mock_ctx: MagicMock) -> None:
        """Verifies the function delegates to session_manager.resolve_session_reference."""
        mock_ctx.return_value = {"id": "proj-123"}
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "uuid-abc"

        result = resolve_session_id(mock_sm, "#42")

        assert result == "uuid-abc"
        mock_sm.resolve_session_reference.assert_called_once_with("#42", "proj-123")


# ---------------------------------------------------------------------------
# resolve_session_task_value
# ---------------------------------------------------------------------------


class TestResolveSessionTaskValuePassthrough:
    """Tests for values that should pass through unchanged."""

    def test_non_string_integer_passes_through(self) -> None:
        result = resolve_session_task_value(42, "sess-id", MagicMock(), MagicMock())
        assert result == 42

    def test_non_string_none_passes_through(self) -> None:
        result = resolve_session_task_value(None, "sess-id", MagicMock(), MagicMock())
        assert result is None

    def test_non_string_list_passes_through(self) -> None:
        val = ["a", "b"]
        result = resolve_session_task_value(val, "sess-id", MagicMock(), MagicMock())
        assert result == val

    def test_non_string_bool_passes_through(self) -> None:
        result = resolve_session_task_value(True, "sess-id", MagicMock(), MagicMock())
        assert result is True

    def test_uuid_string_passes_through(self) -> None:
        """UUID-like strings don't start with # and aren't digits, so they pass through."""
        uuid_val = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        result = resolve_session_task_value(uuid_val, "sess-id", MagicMock(), MagicMock())
        assert result == uuid_val

    def test_text_string_passes_through(self) -> None:
        """Non-numeric, non-# strings pass through."""
        result = resolve_session_task_value("some-text", "sess-id", MagicMock(), MagicMock())
        assert result == "some-text"

    def test_empty_string_passes_through(self) -> None:
        """Empty string is not # nor all-digit, so it passes through."""
        result = resolve_session_task_value("", "sess-id", MagicMock(), MagicMock())
        assert result == ""


class TestResolveSessionTaskValueHashRef:
    """Tests for #N format references."""

    def test_hash_ref_resolved_with_real_db(
        self,
        temp_db: LocalDatabase,
        real_session_manager: LocalSessionManager,
        sample_project: dict,
        sample_session: MagicMock,
    ) -> None:
        """Use real DB to create a task, then resolve its #N reference."""
        from gobby.storage.tasks import LocalTaskManager

        tm = LocalTaskManager(temp_db)
        task = tm.create_task(
            project_id=sample_project["id"],
            title="Test task for resolution",
        )

        result = resolve_session_task_value(
            f"#{task.seq_num}",
            sample_session.id,
            real_session_manager,
            temp_db,
        )

        assert result == task.id

    def test_numeric_ref_resolved_with_real_db(
        self,
        temp_db: LocalDatabase,
        real_session_manager: LocalSessionManager,
        sample_project: dict,
        sample_session: MagicMock,
    ) -> None:
        """Bare numeric string (e.g., '42') is also resolved."""
        from gobby.storage.tasks import LocalTaskManager

        tm = LocalTaskManager(temp_db)
        task = tm.create_task(
            project_id=sample_project["id"],
            title="Task for numeric ref",
        )

        result = resolve_session_task_value(
            str(task.seq_num),
            sample_session.id,
            real_session_manager,
            temp_db,
        )

        assert result == task.id


class TestResolveSessionTaskValueNoSession:
    """Tests when session_id is missing or session lookup fails."""

    def test_no_session_id_returns_original(self) -> None:
        result = resolve_session_task_value("#42", None, MagicMock(), MagicMock())
        assert result == "#42"

    def test_session_not_found_returns_original(
        self,
        real_session_manager: LocalSessionManager,
        temp_db: LocalDatabase,
    ) -> None:
        result = resolve_session_task_value(
            "#42",
            "nonexistent-session-id",
            real_session_manager,
            temp_db,
        )
        assert result == "#42"

    def test_session_has_no_project_id_returns_original(self) -> None:
        """When session exists but has no project_id."""
        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = None
        mock_sm.get.return_value = mock_session

        result = resolve_session_task_value("#42", "sess-id", mock_sm, MagicMock())
        assert result == "#42"


class TestResolveSessionTaskValueErrors:
    """Tests for error handling during resolution."""

    def test_task_not_found_returns_original(self) -> None:
        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        mock_db = MagicMock()

        with patch(
            "gobby.mcp_proxy.tools.workflows._resolution.resolve_task_reference",
            side_effect=TaskNotFoundError("Not found"),
        ):
            result = resolve_session_task_value("#999", "sess-id", mock_sm, mock_db)
            assert result == "#999"

    def test_unexpected_error_returns_original(self) -> None:
        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        mock_db = MagicMock()

        with patch(
            "gobby.mcp_proxy.tools.workflows._resolution.resolve_task_reference",
            side_effect=RuntimeError("boom"),
        ):
            result = resolve_session_task_value("#42", "sess-id", mock_sm, mock_db)
            assert result == "#42"

    def test_type_error_returns_original(self) -> None:
        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        mock_db = MagicMock()

        with patch(
            "gobby.mcp_proxy.tools.workflows._resolution.resolve_task_reference",
            side_effect=TypeError("bad type"),
        ):
            result = resolve_session_task_value("#42", "sess-id", mock_sm, mock_db)
            assert result == "#42"


class TestResolveSessionTaskValueNumericRef:
    """Additional tests for numeric (non-hash) reference handling."""

    def test_numeric_zero_is_seq_ref(self) -> None:
        """'0' is all digits so is treated as a seq_num reference."""
        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        with patch(
            "gobby.mcp_proxy.tools.workflows._resolution.resolve_task_reference",
            side_effect=TaskNotFoundError("Not found"),
        ):
            result = resolve_session_task_value("0", "sess-id", mock_sm, MagicMock())
            # Returns original on not found
            assert result == "0"

    def test_hash_only_is_treated_as_seq_ref(self) -> None:
        """'#' starts with # so is_seq_ref is True."""
        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        with patch(
            "gobby.mcp_proxy.tools.workflows._resolution.resolve_task_reference",
            side_effect=TaskNotFoundError("Invalid"),
        ):
            result = resolve_session_task_value("#", "sess-id", mock_sm, MagicMock())
            assert result == "#"
