"""Claim projection declares every variable the extra-skill refresh writes."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.events import SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000012"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_merge_claimed_task_projection_declares_tdd_obligations(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase, tmp_path: Path
) -> None:
    """A claim observation persists the TDD obligations the refresh derives (#24766)."""
    project = isolated_checkout_factory(temp_db, "claim-projection", root=tmp_path).project
    session_id = SessionManager(temp_db).register_session(
        external_id="claim-projection-external",
        machine_id=LOCAL_MACHINE_ID,
        source=SessionSource.CLAUDE.value,
        project_id=project.id,
        project_path=str(tmp_path),
    )
    assert session_id
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="Exercise claim projection",
        claimed_by_session_id=session_id,
        category="code",
        implementation_domain="backend",
        labels=["tdd:required"],
        validation_criteria="Projection tests pass.",
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(session_id, {"enforce_tdd": True})

    assert variables.merge_variables(
        session_id, {"task_claimed": True}, observed_claim_task_id=task.id
    )

    stored = variables.get_variable_subset(session_id, ("claimed_task_tdd_obligations",))
    assert set(stored["claimed_task_tdd_obligations"]) == {task.id}
