from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.servers.middleware.project_context import ProjectContextMiddleware
from gobby.utils.project_context import get_project_context, reset_project_context

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _request(headers: dict[str, str], state: SimpleNamespace) -> MagicMock:
    request = MagicMock()
    request.headers = headers
    request.app.state = state
    return request


async def test_session_context_lookups_use_server_db_executor() -> None:
    session_manager = MagicMock()
    session_manager.db = MagicMock()
    session = SimpleNamespace(project_id="project-1")
    project = SimpleNamespace(id="project-1", name="Test", repo_path="/repo")
    server = SimpleNamespace(run_db=AsyncMock(side_effect=[session, project]))
    request = _request(
        {"x-gobby-session-id": "session-1"},
        SimpleNamespace(session_manager=session_manager, server=server),
    )
    middleware = ProjectContextMiddleware(AsyncMock())

    with patch("gobby.storage.projects.LocalProjectManager") as manager_class:
        project_manager = manager_class.return_value
        token = await middleware._set_context(request)

    assert token is not None
    try:
        assert get_project_context() == {
            "id": "project-1",
            "name": "Test",
            "project_path": "/repo",
        }
        assert server.run_db.await_args_list == [
            call(session_manager.get, "session-1"),
            call(project_manager.get, "project-1"),
        ]
    finally:
        reset_project_context(token)


async def test_project_context_lookup_uses_thread_fallback_without_server() -> None:
    session_manager = MagicMock()
    session_manager.db = MagicMock()
    project = SimpleNamespace(id="project-1", name="Test", repo_path="/repo")
    request = _request(
        {"x-gobby-project-id": "project-1"},
        SimpleNamespace(session_manager=session_manager),
    )
    middleware = ProjectContextMiddleware(AsyncMock())

    with (
        patch("gobby.storage.projects.LocalProjectManager") as manager_class,
        patch(
            "gobby.servers.middleware.project_context.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=project,
        ) as to_thread,
    ):
        project_manager = manager_class.return_value
        token = await middleware._set_context(request)

    assert token is not None
    try:
        assert get_project_context() == {
            "id": "project-1",
            "name": "Test",
            "project_path": "/repo",
        }
        manager_class.assert_called_once_with(session_manager.db)
        to_thread.assert_awaited_once_with(project_manager.get, "project-1")
    finally:
        reset_project_context(token)
