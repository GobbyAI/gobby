from unittest.mock import Mock

import pytest

from gobby.utils.session_refs import try_resolve_session_field

pytestmark = pytest.mark.unit


def test_rejects_multiple_hash_prefixes() -> None:
    manager = Mock()
    container = {"session_id": "##42"}

    resolved = try_resolve_session_field(
        container,
        "session_id",
        session_manager=manager,
        project_id=None,
    )

    assert resolved is False
    assert container["session_id"] == "##42"
    manager.resolve_session_reference.assert_not_called()


def test_resolves_single_hash_prefix() -> None:
    manager = Mock()
    manager.resolve_session_reference.return_value = "session-uuid"
    container = {"session_id": "#42"}

    resolved = try_resolve_session_field(
        container,
        "session_id",
        session_manager=manager,
        project_id="project-uuid",
    )

    assert resolved is True
    assert container["session_id"] == "session-uuid"
    manager.resolve_session_reference.assert_called_once_with("#42", "project-uuid")


def test_resolves_project_qualified_ref() -> None:
    manager = Mock()
    manager.resolve_session_reference.return_value = "session-uuid"
    container = {"session_id": "game-goblins-S#9"}

    resolved = try_resolve_session_field(
        container,
        "session_id",
        session_manager=manager,
        project_id="project-uuid",
    )

    assert resolved is True
    assert container["session_id"] == "session-uuid"
    manager.resolve_session_reference.assert_called_once_with("game-goblins-S#9", "project-uuid")


def test_leaves_non_ref_strings_untouched() -> None:
    manager = Mock()
    container = {"session_id": "not-a-ref"}

    resolved = try_resolve_session_field(
        container,
        "session_id",
        session_manager=manager,
        project_id=None,
    )

    assert resolved is False
    assert container["session_id"] == "not-a-ref"
    manager.resolve_session_reference.assert_not_called()
