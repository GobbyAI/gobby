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
    assert ("security-review" in (tasks.get_task(task.id).labels or [])) is not (authorized and enabled)
