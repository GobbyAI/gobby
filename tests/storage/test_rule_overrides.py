"""Tests for rule_overrides table CRUD operations."""

from __future__ import annotations

import uuid

import pytest

from gobby.storage.database import LocalDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    """Create a fresh database with migrations applied."""
    db_path = tmp_path / "test_rule_overrides.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


class TestRuleOverridesTable:
    def test_table_exists(self, db: LocalDatabase) -> None:
        """rule_overrides table should exist after migrations."""
        row = db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rule_overrides'"
        )
        assert row is not None

    def test_insert_override(self, db: LocalDatabase) -> None:
        """Should be able to insert a rule override."""
        override_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (override_id, session_id, "require-task-before-edit", 0),
        )

        row = db.fetchone("SELECT * FROM rule_overrides WHERE id = ?", (override_id,))
        assert row is not None
        assert row["session_id"] == session_id
        assert row["rule_name"] == "require-task-before-edit"
        assert row["enabled"] == 0

    def test_query_by_session_and_rule(self, db: LocalDatabase) -> None:
        """Should be able to query overrides by session_id and rule_name."""
        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), session_id, "rule-a", 0),
        )
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), session_id, "rule-b", 1),
        )

        row = db.fetchone(
            "SELECT * FROM rule_overrides WHERE session_id = ? AND rule_name = ?",
            (session_id, "rule-a"),
        )
        assert row is not None
        assert row["enabled"] == 0

        row = db.fetchone(
            "SELECT * FROM rule_overrides WHERE session_id = ? AND rule_name = ?",
            (session_id, "rule-b"),
        )
        assert row is not None
        assert row["enabled"] == 1

    def test_unique_constraint(self, db: LocalDatabase) -> None:
        """session_id + rule_name should be unique."""
        import sqlite3

        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), session_id, "rule-a", 0),
        )

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
                   VALUES (?, ?, ?, ?)""",
                (str(uuid.uuid4()), session_id, "rule-a", 1),
            )

    def test_different_sessions_same_rule(self, db: LocalDatabase) -> None:
        """Different sessions can override the same rule independently."""
        session_a = str(uuid.uuid4())
        session_b = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), session_a, "rule-x", 0),
        )
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), session_b, "rule-x", 1),
        )

        rows = db.fetchall("SELECT * FROM rule_overrides WHERE rule_name = ?", ("rule-x",))
        assert len(rows) == 2

    def test_list_overrides_for_session(self, db: LocalDatabase) -> None:
        """Should list all overrides for a given session."""
        session_id = str(uuid.uuid4())
        other_session = str(uuid.uuid4())

        for name in ("rule-a", "rule-b", "rule-c"):
            db.execute(
                """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
                   VALUES (?, ?, ?, ?)""",
                (str(uuid.uuid4()), session_id, name, 0),
            )
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (str(uuid.uuid4()), other_session, "rule-d", 1),
        )

        rows = db.fetchall("SELECT * FROM rule_overrides WHERE session_id = ?", (session_id,))
        assert len(rows) == 3

    def test_created_at_default(self, db: LocalDatabase) -> None:
        """created_at should be auto-populated."""
        override_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (override_id, str(uuid.uuid4()), "rule-a", 0),
        )

        row = db.fetchone("SELECT * FROM rule_overrides WHERE id = ?", (override_id,))
        assert row["created_at"] is not None

    def test_delete_override(self, db: LocalDatabase) -> None:
        """Should be able to delete an override."""
        override_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (?, ?, ?, ?)""",
            (override_id, session_id, "rule-a", 0),
        )

        db.execute("DELETE FROM rule_overrides WHERE id = ?", (override_id,))

        row = db.fetchone("SELECT * FROM rule_overrides WHERE id = ?", (override_id,))
        assert row is None
