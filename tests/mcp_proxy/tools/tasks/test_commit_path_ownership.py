"""Commit admission refuses another active claim's live checkout paths."""

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.commit_ownership import assert_task_commit_paths_available
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


def test_commit_paths_cannot_cross_another_active_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Commit paths have one active owner."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Commit paths have one active owner."
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    variables.record_edited_files(
        canonical_task_session.id, ["src/first.py"], checkout_root=str(tmp_path)
    )
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)

    with pytest.raises(ValueError, match=f"src/first.py.*#{first.seq_num}"):
        assert_task_commit_paths_available(tasks, second, {"src/first.py"}, str(tmp_path))

    assert_task_commit_paths_available(tasks, second, {"src/second.py"}, str(tmp_path))
    assert_task_commit_paths_available(tasks, first, {"src/first.py"}, str(tmp_path))
