"""Real-database contracts for destructive isolation cleanup selectors."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from gobby.storage.clones import CloneStatus, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize("task_state", ["open", "claimed", "closed"])
def test_cleanup_requires_closed_linked_task(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    session_manager: SessionManager,
    kind: str,
    task_state: str,
) -> None:
    project_id = str(sample_project["id"])
    session = session_manager.register(
        external_id=f"cleanup-{kind}-{task_state}",
        source="codex",
        machine_id=None,
        project_id=project_id,
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id,
        "Cleanup owner",
        claimed_by_session_id=session.id if task_state == "claimed" else None,
        validation_criteria="Only closed unclaimed task isolation is eligible",
    )
    if task_state == "closed":
        temp_db.execute("UPDATE tasks SET closed_at = NOW() WHERE id = %s", (task.id,))
    manager = LocalWorktreeManager(temp_db) if kind == "worktree" else LocalCloneManager(temp_db)
    workspace = manager.create(project_id, "expired", f"/tmp/{kind}/expired", task_id=task.id)
    manager.update(workspace.id, status="merged", cleanup_after=datetime(2020, 1, 1, tzinfo=UTC))

    selected = [row.id for row in manager.find_expired(project_id)]
    with manager.lock_for_cleanup(workspace.id) as locked:
        if task_state == "closed":
            assert selected == [workspace.id]
            assert locked is not None and locked.id == workspace.id
        else:
            assert selected == []
            assert locked is None


@pytest.mark.parametrize("kind", ["worktree", "clone"])
def test_cleanup_lock_rechecks_claims_and_blocks_reopen_until_release(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    session_manager: SessionManager,
    kind: str,
) -> None:
    project_id = str(sample_project["id"])
    session = session_manager.register(
        external_id=f"cleanup-lock-{kind}", source="codex", machine_id=None, project_id=project_id
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id, "Closed cleanup task", validation_criteria="Claims cannot race cleanup"
    )
    temp_db.execute("UPDATE tasks SET closed_at = NOW() WHERE id = %s", (task.id,))
    manager = LocalWorktreeManager(temp_db) if kind == "worktree" else LocalCloneManager(temp_db)
    workspace = manager.create(project_id, "expired", f"/tmp/{kind}/expired", task_id=task.id)
    manager.update(workspace.id, status="merged", cleanup_after=datetime(2020, 1, 1, tzinfo=UTC))
    assert [row.id for row in manager.find_expired(project_id)] == [workspace.id]

    def reopen_task() -> None:
        with temp_db.bounded_transaction(lock_timeout_ms=50) as conn:
            conn.execute("UPDATE tasks SET closed_at = NULL WHERE id = %s", (task.id,))

    def claim_workspace() -> None:
        with temp_db.bounded_transaction(lock_timeout_ms=50):
            manager.claim(workspace.id, session.id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            with manager.lock_for_cleanup(workspace.id) as locked:
                assert locked is not None
                raise RuntimeError("cleanup failed")
        with manager.lock_for_cleanup(workspace.id) as locked:
            assert locked is not None
            with pytest.raises(psycopg.errors.LockNotAvailable):
                executor.submit(reopen_task).result(timeout=2)
            with pytest.raises(psycopg.errors.LockNotAvailable):
                executor.submit(claim_workspace).result(timeout=2)
        executor.submit(reopen_task).result(timeout=2)

    with manager.lock_for_cleanup(workspace.id) as locked:
        assert locked is None
    temp_db.execute("UPDATE tasks SET closed_at = NOW() WHERE id = %s", (task.id,))
    assert manager.claim(workspace.id, session.id) is not None
    with manager.lock_for_cleanup(workspace.id) as locked:
        assert locked is None


def test_worktree_sweeps_exclude_claimed_and_ineligible_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    session_manager: SessionManager,
) -> None:
    manager = LocalWorktreeManager(temp_db)
    project_id = str(sample_project["id"])
    stale_at = datetime.now(UTC) - timedelta(hours=48)
    expired_at = datetime(2020, 1, 1, tzinfo=UTC)
    session = session_manager.register(
        external_id="worktree-sweep-owner",
        machine_id=None,
        source="codex",
        project_id=project_id,
    )

    stale = manager.create(project_id, "stale", "/tmp/worktrees/stale")
    claimed_stale = manager.create(
        project_id,
        "claimed-stale",
        "/tmp/worktrees/claimed-stale",
        agent_session_id=session.id,
    )
    merged = manager.create(project_id, "merged", "/tmp/worktrees/merged")
    manager.mark_merged(merged.id)
    claimed_merged = manager.create(
        project_id,
        "claimed-merged",
        "/tmp/worktrees/claimed-merged",
        agent_session_id=session.id,
    )
    manager.mark_merged(claimed_merged.id)
    active_expired = manager.create(
        project_id,
        "active-expired",
        "/tmp/worktrees/active-expired",
    )

    temp_db.execute(
        """UPDATE worktrees
           SET updated_at = %s, last_activity_at = %s
           WHERE id IN (%s, %s)""",
        (stale_at, stale_at, stale.id, claimed_stale.id),
    )
    temp_db.execute(
        "UPDATE worktrees SET cleanup_after = %s WHERE id = %s",
        (expired_at, active_expired.id),
    )

    stale_ids = {row.id for row in manager.find_stale(project_id)}
    expired_ids = {row.id for row in manager.find_expired(project_id)}

    assert stale_ids == {stale.id}
    assert expired_ids == {merged.id}


def test_clone_sweeps_exclude_claimed_and_ineligible_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    session_manager: SessionManager,
) -> None:
    manager = LocalCloneManager(temp_db)
    project_id = str(sample_project["id"])
    stale_at = datetime.now(UTC) - timedelta(hours=48)
    expired_at = datetime(2020, 1, 1, tzinfo=UTC)
    session = session_manager.register(
        external_id="clone-sweep-owner",
        machine_id=None,
        source="codex",
        project_id=project_id,
    )

    stale = manager.create(project_id, "stale", "/tmp/clones/stale")
    syncing = manager.create(project_id, "syncing", "/tmp/clones/syncing")
    manager.mark_syncing(syncing.id)
    claimed_stale = manager.create(
        project_id,
        "claimed-stale",
        "/tmp/clones/claimed-stale",
        agent_session_id=session.id,
    )
    merged_stale = manager.create(
        project_id,
        "merged-stale",
        "/tmp/clones/merged-stale",
    )
    manager.mark_merged(merged_stale.id)
    merged = manager.create(project_id, "merged", "/tmp/clones/merged")
    manager.mark_merged(merged.id, cleanup_after=expired_at)
    claimed_merged = manager.create(
        project_id,
        "claimed-merged",
        "/tmp/clones/claimed-merged",
        agent_session_id=session.id,
    )
    manager.update(
        claimed_merged.id,
        status=CloneStatus.MERGED.value,
        cleanup_after=expired_at,
    )
    active_expired = manager.create(
        project_id,
        "active-expired",
        "/tmp/clones/active-expired",
        cleanup_after=expired_at,
    )

    temp_db.execute(
        "UPDATE clones SET updated_at = %s WHERE id IN (%s, %s, %s, %s)",
        (stale_at, stale.id, syncing.id, claimed_stale.id, merged_stale.id),
    )

    stale_ids = {row.id for row in manager.find_stale(project_id)}
    expired_ids = {row.id for row in manager.find_expired(project_id)}

    assert stale_ids == {stale.id, syncing.id}
    assert expired_ids == {merged.id}
    assert active_expired.id not in expired_ids
    assert claimed_merged.id not in expired_ids
    assert claimed_stale.id not in stale_ids
    assert merged_stale.id not in stale_ids
