from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.tasks._utils import resolve_task_id

pytestmark = pytest.mark.unit


def test_resolve_numeric_string_as_seq_num() -> None:
    """Test that a numeric string '123' is treated as '#123'."""
    mock_manager = MagicMock()

    # Setup mock to return a task when resolve_task_reference is called with '#123'
    expected_uuid = "uuid-123"
    mock_manager.resolve_task_reference.return_value = expected_uuid

    mock_task = MagicMock()
    mock_task.id = expected_uuid
    mock_manager.get_task.return_value = mock_task

    # Call with pure numeric string
    result = resolve_task_id(mock_manager, "123", project_id="proj-1")

    # Verify it called resolve_task_reference with '#' prefixed
    mock_manager.resolve_task_reference.assert_called_with("#123", "proj-1")
    assert result == mock_task


def test_resolve_numeric_string_fails_if_no_project() -> None:
    """Test that numeric string resolution fails gracefully if no project_id."""
    mock_manager = MagicMock()
    mock_manager.get_task.side_effect = ValueError("Invalid UUID")

    # Should return None if no project context and no project_id passed
    with patch("gobby.cli.tasks._utils.resolution.get_project_context", return_value=None):
        result = resolve_task_id(mock_manager, "123", project_id=None)

    assert result is None
