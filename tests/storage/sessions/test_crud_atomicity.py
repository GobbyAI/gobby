"""Focused tests for session CRUD atomicity."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def test_register_assigns_unique_seq_nums_under_concurrency(
    temp_db: HubDatabase,
    sample_project: dict[str, str],
) -> None:
    barrier = threading.Barrier(4)
    seq_nums: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _register(index: int) -> None:
        manager = SessionManager(temp_db)
        try:
            barrier.wait(timeout=5)
            session = manager.register(
                external_id=f"concurrent-{index}",
                machine_id="machine-1",
                source="claude",
                project_id=sample_project["id"],
            )
            with lock:
                seq_nums.append(session.seq_num)
        except BaseException as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_register, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(seq_nums) == [1, 2, 3, 4]


def test_create_web_chat_session_rolls_back_when_follow_up_update_fails(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    original_execute = session_manager.db.execute

    def fail_on_model_update(sql: str, params: tuple[object, ...] = ()) -> object:
        if "SET model = COALESCE(%s, model)" in sql:
            raise RuntimeError("boom")
        return original_execute(sql, params)

    with patch.object(session_manager.db, "execute", side_effect=fail_on_model_update):
        with pytest.raises(RuntimeError, match="boom"):
            session_manager.create_web_chat_session(
                machine_id="machine-1",
                project_id=sample_project["id"],
                source="claude",
                title="Web Chat",
                model="claude-opus-4-5-20251101",
                sandbox_enabled=True,
                sandbox_policy_hash="policy-hash-123",
            )

    assert session_manager.list(project_id=sample_project["id"]) == []


def test_recalculate_stats_short_circuits_for_missing_session(
    session_manager: SessionManager,
) -> None:
    with patch.object(session_manager.db, "execute", wraps=session_manager.db.execute) as execute:
        result = session_manager.recalculate_stats("missing-session")

    assert result is None
    execute.assert_not_called()


def test_update_chat_mode_refreshes_updated_at(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session = session_manager.register(
        external_id="chat-mode-updated-at",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    stale_timestamp = "2000-01-01T00:00:00+00:00"
    session_manager.db.execute(
        "UPDATE sessions SET updated_at = %s WHERE id = %s",
        (stale_timestamp, session.id),
    )

    session_manager.update_chat_mode(session.id, "bypass")

    row = session_manager.db.fetchone(
        "SELECT chat_mode, updated_at FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None
    assert row["chat_mode"] == "bypass"
    assert row["updated_at"] != stale_timestamp


def test_update_approved_tools_refreshes_updated_at(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session = session_manager.register(
        external_id="approved-tools-updated-at",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    stale_timestamp = "2000-01-01T00:00:00+00:00"
    session_manager.db.execute(
        "UPDATE sessions SET updated_at = %s WHERE id = %s",
        (stale_timestamp, session.id),
    )

    session_manager.update_approved_tools(session.id, {"functions.exec_command"})

    row = session_manager.db.fetchone(
        "SELECT approved_tools_json, updated_at FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None
    assert row["approved_tools_json"] == '["functions.exec_command"]'
    assert row["updated_at"] != stale_timestamp
