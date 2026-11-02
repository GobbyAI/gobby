"""Every storage mutation preserves mandatory review labels."""

from typing import Any

import pytest

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._updates import update_task
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def test_operator_scope_is_explicit_and_does_not_leak(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.storage.tasks._label_policy import operator_label_changes

    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        sample_project["id"], "Operator review decision", labels=["security-review"]
    )
    with operator_label_changes():
        tasks.remove_label(task.id, "security-review")
    assert not tasks.get_task(task.id).labels
    tasks.add_label(task.id, "security-review")
    with pytest.raises(ValueError, match="security-review"):
        tasks.remove_label(task.id, "security-review")


def test_operator_cli_can_remove_review_label(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from gobby.cli.tasks.labels import label_cmd

    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        sample_project["id"], "Operator CLI decision", labels=["security-review"]
    )
    monkeypatch.setattr("gobby.cli.tasks.labels.get_task_manager", lambda: tasks)
    result = CliRunner().invoke(label_cmd, ["remove", task.id, "security-review"])
    assert result.exit_code == 0, result.output
    assert not tasks.get_task(task.id).labels


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True])
async def test_rest_uses_authenticated_principal_for_label_removal(
    temp_db: HubDatabase, sample_project: dict[str, Any], operator: bool
) -> None:
    from collections.abc import Callable
    from types import SimpleNamespace
    from typing import cast

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from gobby.servers.routes.tasks import create_tasks_router
    from gobby.storage.sessions import SessionManager

    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        sample_project["id"], "HTTP review decision", labels=["security-review"]
    )

    async def run_db[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        import asyncio

        return await asyncio.to_thread(fn, *args, **kwargs)

    server = SimpleNamespace(
        task_manager=tasks,
        session_manager=SessionManager(temp_db),
        run_db=run_db,
        services=SimpleNamespace(websocket_server=None),
        auth_service=SimpleNamespace(request_principal=lambda request: None if operator else False),
    )
    app = FastAPI()
    app.include_router(create_tasks_router(cast(Any, server)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(f"/api/tasks/{task.id}", json={"labels": []})
    assert response.status_code == (200 if operator else 400), response.text
    assert ("security-review" in (tasks.get_task(task.id).labels or [])) is not operator


@pytest.mark.parametrize("operation", ["replace", "remove", "reconcile"])
@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_security_review_removal_is_authorized_at_storage_boundary(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    operation: str,
    authorized: bool,
    enabled: bool,
) -> None:
    project = sample_project["id"]
    actor = AgentSessions(temp_db, project).add(
        "label-editor", capabilities=("remove_security_review_label",) if authorized else ()
    )
    if not enabled:
        definitions = AgentDefinitionManager(temp_db)
        definition = definitions.get_by_name("label-editor", project)
        assert definition is not None
        definitions.update(definition.id, enabled=False)
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(project, "Protect boundary", labels=["security-review", "other"])

    def remove() -> None:
        if operation == "remove":
            tasks.remove_label(task.id, "security-review")
        elif operation == "replace":
            tasks.update_task(task.id, labels=["other"])
        else:
            update_task(temp_db, task.id, remove_labels=["security-review"])

    with session_context_for_test(session_id=actor):
        if authorized and enabled:
            remove()
        else:
            with pytest.raises(ValueError, match="security-review"):
                remove()
    assert ("security-review" in (tasks.get_task(task.id).labels or [])) is not (
        authorized and enabled
    )
