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
from gobby.tasks.commit_ownership import (
    assert_task_commit_paths_available,
    assert_task_commit_paths_available_async,
)
from gobby.tasks.commits import auto_link_commits_async
from gobby.utils.daemon_git import GitOk
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("targeted", [True, False])
@pytest.mark.parametrize("explicit_cwd", [True, False])
async def test_auto_link_admission_preserves_other_claim_paths(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    targeted: bool,
    explicit_cwd: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(
        sample_project["id"], "First", validation_criteria="Auto-link preserves ownership."
    )
    second = tasks.create_task(
        sample_project["id"], "Second", validation_criteria="Auto-link preserves ownership."
    )
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    SessionVariableManager(temp_db).record_edited_files(
        canonical_task_session.id, ["src/first.py"], checkout_root=str(tmp_path)
    )
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    sha = "b" * 40
    history = GitOk("ok", (), f"{sha}|[gobby-#{second.seq_num}] fix: candidate", "")
    with patch("gobby.tasks.commits.daemon_git.run", new_callable=AsyncMock) as run:
        run.side_effect = [
            history,
            GitOk("ok", (), "src/first.py\0", ""),
            GitOk("ok", (), "9999999999\0src/first.py\0", ""),
        ]
        refused = await auto_link_commits_async(
            tasks,
            task_id=second.id if targeted else None,
            cwd=tmp_path if explicit_cwd else None,
            project_name="gobby",
            project_id=sample_project["id"],
        )
        assert refused.total_linked == 0
        assert refused.skipped == 1
        assert tasks.get_task(second.id).commits in (None, [])

        run.side_effect = [history, GitOk("ok", (), "src/second.py\0", "")]
        accepted = await auto_link_commits_async(
            tasks,
            task_id=second.id if targeted else None,
            cwd=tmp_path if explicit_cwd else None,
            project_name="gobby",
            project_id=sample_project["id"],
        )
        assert accepted.total_linked == 1
        assert tasks.get_task(second.id).commits == [sha]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_edit,release,accepted", [(100, False, False), (300, False, True), (100, True, True)]
)
async def test_commit_chronology_uses_first_live_edit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    first_edit: int,
    release: bool,
    accepted: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(sample_project["id"], "First", validation_criteria="Own commit.")
    second = tasks.create_task(sample_project["id"], "Second", validation_criteria="Live edits.")
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    variables.record_edited_files(
        canonical_task_session.id, ["shared.py"], checkout_root=str(tmp_path), edited_at=first_edit
    )
    if release:
        variables.release_task_edited_files(
            canonical_task_session.id, second.id, ["shared.py"], checkout_root=str(tmp_path)
        )
        variables.record_edited_files(
            canonical_task_session.id, ["shared.py"], checkout_root=str(tmp_path), edited_at=300
        )
    variables.record_edited_files(
        canonical_task_session.id, ["shared.py"], checkout_root=str(tmp_path), edited_at=400
    )
    with patch(
        "gobby.tasks.commit_ownership.daemon_git.run",
        new_callable=AsyncMock,
        return_value=GitOk("ok", (), "200\0\nshared.py\0", ""),
    ):
        if accepted:
            await assert_task_commit_paths_available_async(
                tasks, first, {"shared.py"}, str(tmp_path), ["a" * 40]
            )
        else:
            with pytest.raises(ValueError, match=f"shared.py.*#{second.seq_num}"):
                await assert_task_commit_paths_available_async(
                    tasks, first, {"shared.py"}, str(tmp_path), ["a" * 40]
                )
    assert variables.get_variables(canonical_task_session.id)["task_edited_files"] == {
        second.id: ["shared.py"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [True, False])
async def test_linking_prior_commit_allows_later_other_task_edits(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    automatic: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(sample_project["id"], "First", validation_criteria="Link isolation.")
    second = tasks.create_task(sample_project["id"], "Second", validation_criteria="Live edits.")
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    tasks.claim_task_for_agent(second.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    variables.record_edited_files(
        canonical_task_session.id, ["shared.py"], checkout_root=str(tmp_path), edited_at=300
    )
    sha = "a" * 40
    with patch("gobby.tasks.commit_ownership.daemon_git.run", new_callable=AsyncMock) as run:
        if automatic:
            run.side_effect = [
                GitOk("ok", (), f"{sha}|[gobby-#{first.seq_num}] fix: prior commit", ""),
                GitOk("ok", (), "shared.py\0", ""),
                GitOk("ok", (), "200\0\nshared.py\0", ""),
            ]
            result = await auto_link_commits_async(
                tasks,
                task_id=first.id,
                cwd=tmp_path,
                project_name="gobby",
                project_id=sample_project["id"],
            )
            assert result.total_linked == 1
        else:
            run.return_value = GitOk("ok", (), "200\0\nshared.py\0", "")
            with (
                session_context_for_test(canonical_task_session.id),
                patch(
                    "gobby.mcp_proxy.tools.task_commits.resolve_task_repo_path",
                    return_value=str(tmp_path),
                ),
                patch(
                    "gobby.mcp_proxy.tools.task_commits.normalize_commit_sha",
                    new_callable=AsyncMock,
                    return_value=sha,
                ),
                patch(
                    "gobby.mcp_proxy.tools.tasks._task_scope.collect_net_commit_paths_async",
                    new_callable=AsyncMock,
                    return_value=NetCommitPaths(changed=frozenset({"shared.py"})),
                ),
            ):
                registry = create_commit_registry(tasks, session_manager=SessionManager(temp_db))
                linked = await registry.call(
                    "link_commit", {"task_id": first.id, "commit_sha": sha}
                )
                assert linked["commits"] == [sha]
    assert tasks.get_task(first.id).commits == [sha]
    assert variables.get_variables(canonical_task_session.id)["task_edited_files"] == {
        second.id: ["shared.py"]
    }
