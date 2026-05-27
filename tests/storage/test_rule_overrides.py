"""Tests for rule_overrides table CRUD operations."""

from __future__ import annotations

import uuid

import pytest
from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a fresh database with migrations applied."""
    database = temp_db
    return database


class TestRuleOverridesTable:
    def test_table_exists(self, db: HubDatabase) -> None:
        """rule_overrides table should exist after migrations."""
        row = db.fetchone(
            "SELECT table_name FROM information_schema.tables WHERE table_name = %s",
            ("rule_overrides",),
        )
        assert row is not None

    def test_insert_override(self, db: HubDatabase) -> None:
        """Should be able to insert a rule override."""
        override_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (override_id, session_id, "require-task-before-edit", False),
        )

        row = db.fetchone("SELECT * FROM rule_overrides WHERE id = %s", (override_id,))
        assert row is not None
        assert row["session_id"] == session_id
        assert row["rule_name"] == "require-task-before-edit"
        assert row["enabled"] is False

    def test_query_by_session_and_rule(self, db: HubDatabase) -> None:
        """Should be able to query overrides by session_id and rule_name."""
        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (str(uuid.uuid4()), session_id, "rule-a", False),
        )
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (str(uuid.uuid4()), session_id, "rule-b", True),
        )

        row = db.fetchone(
            "SELECT * FROM rule_overrides WHERE session_id = %s AND rule_name = %s",
            (session_id, "rule-a"),
        )
        assert row is not None
        assert row["enabled"] is False

        row = db.fetchone(
            "SELECT * FROM rule_overrides WHERE session_id = %s AND rule_name = %s",
            (session_id, "rule-b"),
        )
        assert row is not None
        assert row["enabled"] is True

    def test_unique_constraint(self, db: HubDatabase) -> None:
        """session_id + rule_name should be unique."""
        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (str(uuid.uuid4()), session_id, "rule-a", False),
        )

        with pytest.raises(UniqueViolation):
            db.execute(
                """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
                   VALUES (%s, %s, %s, %s)""",
                (str(uuid.uuid4()), session_id, "rule-a", True),
            )

    def test_different_sessions_same_rule(self, db: HubDatabase) -> None:
        """Different sessions can override the same rule independently."""
        session_a = str(uuid.uuid4())
        session_b = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (str(uuid.uuid4()), session_a, "rule-x", False),
        )
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (str(uuid.uuid4()), session_b, "rule-x", True),
        )

        rows = db.fetchall("SELECT * FROM rule_overrides WHERE rule_name = %s", ("rule-x",))
        assert len(rows) == 2

    def test_list_overrides_for_session(self, db: HubDatabase) -> None:
        """Should list all overrides for a given session."""
        session_id = str(uuid.uuid4())
        other_session = str(uuid.uuid4())

        for name in ("rule-a", "rule-b", "rule-c"):
            db.execute(
                """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
                   VALUES (%s, %s, %s, %s)""",
                (str(uuid.uuid4()), session_id, name, False),
            )
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (str(uuid.uuid4()), other_session, "rule-d", True),
        )

        rows = db.fetchall("SELECT * FROM rule_overrides WHERE session_id = %s", (session_id,))
        assert len(rows) == 3

    def test_created_at_default(self, db: HubDatabase) -> None:
        """created_at should be auto-populated."""
        override_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (override_id, str(uuid.uuid4()), "rule-a", False),
        )

        row = db.fetchone("SELECT * FROM rule_overrides WHERE id = %s", (override_id,))
        assert row["created_at"] is not None

    def test_delete_override(self, db: HubDatabase) -> None:
        """Should be able to delete an override."""
        override_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        db.execute(
            """INSERT INTO rule_overrides (id, session_id, rule_name, enabled)
               VALUES (%s, %s, %s, %s)""",
            (override_id, session_id, "rule-a", False),
        )

        db.execute("DELETE FROM rule_overrides WHERE id = %s", (override_id,))

        row = db.fetchone("SELECT * FROM rule_overrides WHERE id = %s", (override_id,))
        assert row is None
