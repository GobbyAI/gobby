"""The bundled docs seat ends only after its declared task completion handoff."""

from pathlib import Path

import pytest
import yaml

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from tests.workflows.test_seat_step_loops import COORDINATOR_ID, SESSION_ID, SeatRun

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "handoff", ["assigned", "other-task", "missing-metadata", "ordinary-report"]
)
async def test_tech_writer_completion_uses_explicit_task_metadata(
    temp_db: HubDatabase, handoff: str
) -> None:
    path = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/agents/tech-writer.yaml"
    definition = yaml.safe_load(path.read_text())
    run = SeatRun(temp_db, "tech-writer", definition=definition)
    task = run.task(owner=SESSION_ID)
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.current_step = "implement"
    instance.variables.update(assigned_task_id=task.id, assigned_task_uuid=task.id)
    run.instances.save(instance)
    metadata = {
        "task_id": task.id if handoff != "other-task" else "another-task",
        "implementation_complete": handoff != "ordinary-report",
    }
    if handoff == "missing-metadata":
        metadata = {}
    content = "Docs are implemented; the evidence IDs and candidate are attached."
    message = InterSessionMessageManager(temp_db).create_message(
        SESSION_ID, COORDINATOR_ID, content
    )
    await run.call(
        "gobby-agents",
        "send_message",
        {"target": "parent", "content": content, "metadata": metadata},
        {"success": True, "message_ids": [message.id]},
    )
    assert run.step == ("terminate" if handoff == "assigned" else "implement")
