"""Real-database contracts for destructive isolation cleanup selectors."""

from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.clones import CloneStatus, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.integration


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
