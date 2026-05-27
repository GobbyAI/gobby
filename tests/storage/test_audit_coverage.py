from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_audit import WorkflowAuditManager

pytestmark = pytest.mark.unit


@pytest.fixture
def test_db(temp_db: HubDatabase) -> HubDatabase:
    """Create a temporary database for testing."""
    return temp_db


@pytest.fixture
def audit_manager(test_db):
    """Create an audit manager instance."""
    return WorkflowAuditManager(test_db)


def _ensure_session(db: HubDatabase, session_id: str) -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, NOW(), NOW())
        ON CONFLICT (id) DO NOTHING
        """,
        ("audit-project", "Audit Project"),
    )
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (session_id, session_id, "test-machine", "test", "audit-project"),
    )


def test_log_basic_entry(audit_manager) -> None:
    """Test logging a basic entry."""
    _ensure_session(audit_manager.db, "sess-1")
    row_id = audit_manager.log(
        session_id="sess-1", step="plan", event_type="tool_call", result="allow", reason="whitelist"
    )
    assert row_id is not None

    count = audit_manager.get_entry_count()
    assert count == 1

    entries = audit_manager.get_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.session_id == "sess-1"
    assert entry.step == "plan"
    assert entry.result == "allow"
    assert entry.reason == "whitelist"


def test_log_helpers(audit_manager) -> None:
    """Test helper logging methods."""
    _ensure_session(audit_manager.db, "sess-1")
    # log_tool_call
    audit_manager.log_tool_call(
        session_id="sess-1", step="exec", tool_name="read_file", result="block", reason="bad file"
    )

    # log_rule_eval
    audit_manager.log_rule_eval(
        session_id="sess-1", step="exec", rule_id="r1", condition="always", result="allow"
    )

    # log_transition
    audit_manager.log_transition(session_id="sess-1", from_step="plan", to_step="exec")

    # log_exit_check
    audit_manager.log_exit_check(session_id="sess-1", step="exec", condition="done", result="met")

    # log_approval
    audit_manager.log_approval(
        session_id="sess-1", step="check", result="approved", condition_id="c1", prompt="approve?"
    )

    assert audit_manager.get_entry_count() == 5


def test_get_entries_filtering(audit_manager) -> None:
    """Test filtering entries."""
    _ensure_session(audit_manager.db, "s1")
    _ensure_session(audit_manager.db, "s2")
    audit_manager.log(session_id="s1", step="1", event_type="e1", result="allow")
    audit_manager.log(session_id="s2", step="1", event_type="e2", result="block")
    audit_manager.log(session_id="s1", step="1", event_type="e3", result="block")

    # Filter by session
    entries = audit_manager.get_entries(session_id="s1")
    assert len(entries) == 2
    assert all(e.session_id == "s1" for e in entries)

    # Filter by result
    entries = audit_manager.get_entries(result="block")
    assert len(entries) == 2
    assert all(e.result == "block" for e in entries)

    # Limit
    entries = audit_manager.get_entries(limit=1)
    assert len(entries) == 1


def test_cleanup_entries(audit_manager, test_db) -> None:
    """Test cleaning up old entries."""
    _ensure_session(test_db, "old")
    _ensure_session(test_db, "new")
    # Insert old entry manually to bypass generic timestamp usage in log()
    old_time = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    test_db.execute(
        "INSERT INTO workflow_audit_log (session_id, timestamp, step, event_type, result) VALUES (%s, %s, %s, %s, %s)",
        ("old", old_time, "s", "e", "r"),
    )

    # Insert new entry
    audit_manager.log("new", "s", "e", "allow")

    assert audit_manager.get_entry_count() == 2

    deleted = audit_manager.cleanup_old_entries(days=7)
    assert deleted == 1

    assert audit_manager.get_entry_count() == 1
    entries = audit_manager.get_entries()
    assert entries[0].session_id == "new"


def test_log_error_handling(audit_manager) -> None:
    """Test error handling in log method."""
    # Break the DB connection to force error
    # By convention, if we close the connection inside HubDatabase, generic execute might check.
    # But HubDatabase manages connections per execute usually? No, it holds conn.
    # We can mock db.execute to raise Exception.

    original_execute = audit_manager.db.execute
    audit_manager.db.execute = MagicMock(side_effect=Exception("DB Error"))

    row_id = audit_manager.log("s1", "step", "event", "result")
    assert row_id is None

    # Cleanup
    audit_manager.db.execute = original_execute
