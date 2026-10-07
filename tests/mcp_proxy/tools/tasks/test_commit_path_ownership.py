"""Commit admission refuses another active claim's live checkout paths."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.mcp_proxy.tools.tasks._task_scope import NetCommitPaths
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.commit_ownership import assert_task_commit_paths_available
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_link_commit_refuses_other_claim_paths_without_linking(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Link preserves ownership."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Link preserves ownership."
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    SessionVariableManager(temp_db).record_edited_files(
        canonical_task_session.id, ["src/first.py"], checkout_root=str(tmp_path)
    )
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    sha = "a" * 40
    with (
        session_context_for_test(canonical_task_session.id),
        patch(
            "gobby.mcp_proxy.tools.task_commits.resolve_task_repo_path", return_value=str(tmp_path)
        ),
        patch(
            "gobby.mcp_proxy.tools.task_commits.normalize_commit_sha",
            new_callable=AsyncMock,
            return_value=sha,
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._task_scope.collect_net_commit_paths_async",
            new_callable=AsyncMock,
            return_value=NetCommitPaths(changed=frozenset({"src/first.py"})),
        ) as collect,
    ):
        registry = create_commit_registry(tasks, session_manager=SessionManager(temp_db))
        refused = await registry.call("link_commit", {"task_id": second.id, "commit_sha": sha})
        assert "split changes by task" in refused["error"]
        assert f"#{first.seq_num}" in refused["error"]
        assert tasks.get_task(second.id).commits in (None, [])
        collect.return_value = NetCommitPaths(changed=frozenset({"src/second.py"}))
        accepted = await registry.call("link_commit", {"task_id": second.id, "commit_sha": sha})
        assert accepted["commits"] == [sha]


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
