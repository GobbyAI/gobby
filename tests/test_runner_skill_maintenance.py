from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.config.app import DaemonConfig
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_periodic import _default_loops, start_periodic_tasks
from gobby.runner_maintenance import purge_deleted_skills_loop
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager, SkillFile
from gobby.utils.machine_id import require_machine_id
from tests.config_runtime_helpers import static_runtime_capture


@pytest.mark.asyncio
async def test_purge_deleted_skills_removes_only_expired_rows(
    temp_db: HubDatabase,
) -> None:
    manager = LocalSkillManager(temp_db)
    expired = manager.create_skill(
        name="expired-maintenance-skill",
        description="Expired",
        content="# Expired",
    )
    recent = manager.create_skill(
        name="recent-maintenance-skill",
        description="Recent",
        content="# Recent",
    )
    for skill in (expired, recent):
        body = f"# {skill.name} reference"
        manager.set_skill_files(
            skill.id,
            [
                SkillFile(
                    id="",
                    skill_id=skill.id,
                    path="references/info.md",
                    file_type="reference",
                    content=body,
                    content_hash=sha256(body.encode()).hexdigest(),
                    size_bytes=len(body.encode()),
                )
            ],
        )
        assert manager.delete_skill(skill.id) is True

        session = SessionManager(temp_db).register(
            external_id="skill-retention-test",
            machine_id=require_machine_id(),
            source="codex",
            project_id=None,
        )
    now = datetime.now(UTC)
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE skills SET deleted_at = %s WHERE id = %s",
            (now - timedelta(days=31), expired.id),
        )
        conn.execute(
            "UPDATE skills SET deleted_at = %s WHERE id = %s",
            (now - timedelta(days=29), recent.id),
        )
        conn.execute(
            "INSERT INTO session_skills (session_id, skill_name) VALUES (%s, %s)",
            (session.id, expired.name),
        )

    shutdown = iter((False, True))
    sleep = AsyncMock()

    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    await purge_deleted_skills_loop(
        temp_db,
        lambda: next(shutdown),
        capture_bundle=static_runtime_capture(
            DaemonConfig(skills={"soft_delete_retention_days": 30})
        ),
        run_db=run_db,
        startup_delay_seconds=0,
        sleep=sleep,
    )

    assert manager.get_by_name(expired.name, include_deleted=True) is None
    assert manager.get_by_name(recent.name, include_deleted=True) is not None

    expired_files = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM skill_files WHERE skill_id = %s",
        (expired.id,),
    )
    recent_files = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM skill_files WHERE skill_id = %s",
        (recent.id,),
    )
    usage = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_skills WHERE skill_name = %s",
        (expired.name,),
    )
    assert expired_files is not None and expired_files["count"] == 0
    assert recent_files is not None and recent_files["count"] == 1
    assert usage is not None and usage["count"] == 1
    sleep.assert_awaited_once_with(24 * 60 * 60)


@pytest.mark.asyncio
async def test_periodic_start_uses_configured_skill_retention() -> None:
    retention_days: list[int] = []

    async def complete_loop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def capture_skill_purge(*_args: Any, **kwargs: Any) -> None:
        active = kwargs["capture_bundle"]().snapshot.active
        retention_days.append(active.skills.soft_delete_retention_days)

    loops = dict.fromkeys(_default_loops(), complete_loop)
    loops["purge_deleted_skills_loop"] = capture_skill_purge
    active_config = DaemonConfig(skills={"soft_delete_retention_days": 14})
    runner = SimpleNamespace(
        config_runtime=SimpleNamespace(capture=static_runtime_capture(active_config)),
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        db_executor=None,
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        session_manager=None,
        _shutdown_requested=False,
    )

    start_periodic_tasks(cast(GobbyRunner, runner), tracker=None, **loops)
    await asyncio.gather(
        *(task for task in vars(runner).values() if isinstance(task, asyncio.Task))
    )

    assert DaemonConfig().skills.soft_delete_retention_days == 30
    assert retention_days == [14]
