from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from starlette.responses import Response

from gobby.servers.middleware.project_context import (
    ProjectContextMiddleware,
    _project_context_payload,
)
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
    payload = {"id": "project-1", "name": "Test", "project_path": "/repo"}
    server = SimpleNamespace(run_db=AsyncMock(side_effect=[session, project, payload]))
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
        assert get_project_context() == payload
        # The checkout lookup inside the payload builder runs off the loop too.
        assert server.run_db.await_args_list == [
            call(session_manager.get, "session-1"),
            call(project_manager.get, "project-1"),
            call(_project_context_payload, project, session_manager.db, None),
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
            side_effect=lambda func, *args: func(*args),
        ) as to_thread,
        patch("gobby.storage.project_checkouts.require_root", return_value="/repo"),
        patch(
            "gobby.storage.workspace_machine_scope.require_local_machine_id",
            return_value="machine-1",
        ),
    ):
        project_manager = manager_class.return_value
        project_manager.get.return_value = project
        token = await middleware._set_context(request)

    assert token is not None
    try:
        assert get_project_context() == {
            "id": "project-1",
            "name": "Test",
            "project_path": "/repo",
        }
        manager_class.assert_called_once_with(session_manager.db)
        assert to_thread.await_args_list == [
            call(project_manager.get, "project-1"),
            call(_project_context_payload, project, session_manager.db, None),
        ]
    finally:
        reset_project_context(token)


async def test_dispatch_exposes_seeded_context_to_request_handler() -> None:
    session_manager = MagicMock()
    session_manager.db = MagicMock()
    project = SimpleNamespace(id="project-1", name="Test", repo_path="/repo")
    request = _request(
        {"x-gobby-project-id": "project-1"},
        SimpleNamespace(session_manager=session_manager),
    )
    middleware = ProjectContextMiddleware(AsyncMock())
    initial_context = get_project_context()
    observed_context: dict[str, str] | None = None

    async def call_next(_request: MagicMock) -> Response:
        nonlocal observed_context
        observed_context = get_project_context()
        return Response()

    with (
        patch("gobby.storage.projects.LocalProjectManager") as manager_class,
        patch(
            "gobby.servers.middleware.project_context.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=lambda func, *args: func(*args),
        ),
        patch("gobby.storage.project_checkouts.require_root", return_value="/repo"),
        patch(
            "gobby.storage.workspace_machine_scope.require_local_machine_id",
            return_value="machine-1",
        ),
    ):
        manager_class.return_value.get.return_value = project
        await middleware.dispatch(request, call_next)

    assert observed_context == {
        "id": "project-1",
        "name": "Test",
        "project_path": "/repo",
    }
    assert get_project_context() == initial_context


async def test_session_context_uses_machine_checkout(  # tdd-red window
    temp_db: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from gobby.storage.sessions import SessionManager
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, Path(tmp_path) / "repo", monkeypatch=monkeypatch
    )
    session = SessionManager(temp_db).register(
        external_id="ctx-checkout",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=isolated.project.id,
    )
    session_manager = SessionManager(temp_db)
    request = _request(
        {"x-gobby-session-id": session.id},
        SimpleNamespace(session_manager=session_manager),
    )
    middleware = ProjectContextMiddleware(AsyncMock())
    token = await middleware._set_context(request)
    assert token is not None
    try:
        context = get_project_context()
        assert context is not None
        assert context["id"] == isolated.project.id
        assert context["project_path"] == isolated.root_path
    finally:
        reset_project_context(token)


async def test_session_context_omits_path_without_checkout(  # tdd-red window
    temp_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from tests.fixtures.isolated_checkout import (
        insert_isolated_machine,
        patch_local_machine_id,
    )

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="ctx-missing")
    session = SessionManager(temp_db).register(
        external_id="ctx-missing",
        machine_id=machine_id,
        source="codex",
        project_id=project.id,
    )
    request = _request(
        {"x-gobby-session-id": session.id},
        SimpleNamespace(session_manager=SessionManager(temp_db)),
    )
    middleware = ProjectContextMiddleware(AsyncMock())
    token = await middleware._set_context(request)
    assert token is not None
    try:
        context = get_project_context()
        assert context is not None
        assert context["id"] == project.id
        assert not context.get("project_path")
    finally:
        reset_project_context(token)
