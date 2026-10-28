"""Query-plan guards for the daemon's hot PostgreSQL paths (#25294).

The daemon's Postgres burned hundreds of percent CPU on sequential scans of tables whose hot
lookups had no usable index. Each test seeds enough rows for the planner to discriminate an
indexed access path from a scan, runs ``ANALYZE``, and asserts the plan of the statement the
production code actually issues.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.terminals import TerminalManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000005"
SESSION_COUNT = 300
MESSAGE_COUNT = 30_000
SUBSCRIBER_COUNT = 5_000

Statement = tuple[str, tuple[Any, ...]]


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _issued_statements(db: HubDatabase, action: Callable[[], object]) -> list[Statement]:
    """Run ``action`` and return every read statement it sent to ``db``."""
    with (
        patch.object(db, "fetchone", wraps=db.fetchone) as fetchone,
        patch.object(db, "fetchall", wraps=db.fetchall) as fetchall,
    ):
        action()
    calls = [*fetchone.call_args_list, *fetchall.call_args_list]
    return [(call.args[0], tuple(call.args[1]) if len(call.args) > 1 else ()) for call in calls]


def _plan_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


def _scans_of(db: HubDatabase, statement: Statement, relation: str) -> list[dict[str, Any]]:
    sql_text, params = statement
    with psycopg.connect(db.conninfo, autocommit=True) as conn:
        cursor = psycopg.ClientCursor(conn)
        cursor.execute("EXPLAIN (FORMAT JSON) " + sql_text, params)
        row = cursor.fetchone()
    assert row is not None
    nodes = list(_plan_nodes(row[0][0]["Plan"]))
    return [node for node in nodes if node.get("Relation Name") == relation or "Index Name" in node]


def _leading_column(db: HubDatabase, index_name: str) -> str:
    row = db.fetchone(
        """
        SELECT a.attname AS column_name
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
        WHERE i.indexrelid = %s::regclass
        """,
        (index_name,),
    )
    assert row is not None
    return str(row["column_name"])


@pytest.fixture
def subscriber_session(temp_db: HubDatabase) -> str:
    """Seed thousands of subscriber rows, one of which belongs to the returned session."""
    session_id = str(uuid4())
    temp_db.execute(
        """
        INSERT INTO completion_subscribers (completion_id, session_id)
        SELECT gen_random_uuid(), gen_random_uuid() FROM generate_series(1, %s)
        """,
        (SUBSCRIBER_COUNT,),
    )
    temp_db.execute(
        "INSERT INTO completion_subscribers (completion_id, session_id) VALUES (%s, %s)",
        (str(uuid4()), session_id),
    )
    temp_db.execute("ANALYZE completion_subscribers")
    return session_id


def test_completion_subscribers_session_lookup_is_index_served(
    temp_db: HubDatabase, subscriber_session: str
) -> None:
    """``has_active_agent_wait`` runs on every stop-hook gate and filters by session only.

    The primary key leads with ``completion_id`` and the only secondary index covers
    ``completion_id``, so the ``session_id`` filter scanned every subscriber row on each call.
    """
    manager = CompletionSubscriberManager(temp_db)
    issued = _issued_statements(temp_db, lambda: manager.has_active_agent_wait(subscriber_session))
    assert any(subscriber_session in params for _, params in issued), issued

    scans = _scans_of(
        temp_db,
        ("SELECT 1 FROM completion_subscribers WHERE session_id = %s", (subscriber_session,)),
        "completion_subscribers",
    )
    assert scans, "completion_subscribers is not read"
    assert all("Seq Scan" not in node["Node Type"] for node in scans), scans
    assert any(
        _leading_column(temp_db, node["Index Name"]) == "session_id"
        for node in scans
        if "Index Name" in node
    ), scans


@pytest.fixture
def recipient(temp_db: HubDatabase, isolated_checkout_factory: IsolatedCheckoutFactory) -> str:
    """Seed a busy mailbox: many sessions, mostly delivered traffic, a thin undelivered tail."""
    project = isolated_checkout_factory(temp_db, "plan-guard").project
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        SELECT gen_random_uuid(), 'plan-guard-' || g, %s, 'claude', %s
        FROM generate_series(1, %s) g
        """,
        (LOCAL_MACHINE_ID, project.id, SESSION_COUNT),
    )
    temp_db.execute(
        """
        WITH s AS (SELECT array_agg(id) AS ids FROM sessions WHERE external_id LIKE 'plan-guard-%%')
        INSERT INTO inter_session_messages
            (id, from_session, to_session, content, message_type, sent_at, delivered_at,
             metadata_json)
        SELECT gen_random_uuid(),
               s.ids[1 + (g %% %s)],
               s.ids[1 + ((g * 7) %% %s)],
               'plan guard',
               CASE WHEN g %% 10 = 0 THEN 'completion_notification' ELSE 'message' END,
               now() - (g || ' seconds')::interval,
               CASE WHEN g %% 50 = 0 THEN NULL ELSE now() END,
               CASE WHEN g %% 10 = 0
                    THEN '{"completion_id": "c", "wake_requested": "true"}'::jsonb END
        FROM s, generate_series(1, %s) g
        """,
        (SESSION_COUNT, SESSION_COUNT, MESSAGE_COUNT),
    )
    temp_db.execute("ANALYZE inter_session_messages")
    row = temp_db.fetchone("SELECT to_session FROM inter_session_messages LIMIT 1")
    assert row is not None
    return str(row["to_session"])


MAILBOX_READS: dict[str, Callable[[InterSessionMessageManager, str], object]] = {
    "get_messages": lambda manager, session: manager.get_messages(session),
    "get_undelivered_messages": lambda manager, session: manager.get_undelivered_messages(session),
    "get_undelivered_wake_messages": lambda manager, session: (
        manager.get_undelivered_wake_messages(session)
    ),
    "get_undelivered_wake_recipients": lambda manager, _session: (
        manager.get_undelivered_wake_recipients()
    ),
    "has_completion_notification": lambda manager, session: manager.has_completion_notification(
        session, "completion_notification", "c"
    ),
    "has_read_since": lambda manager, session: manager.has_read_since(
        session, datetime(2020, 1, 1, tzinfo=UTC)
    ),
    "list_messages_inbox": lambda manager, session: manager.list_messages(session, "inbox"),
    "list_messages_sent": lambda manager, session: manager.list_messages(session, "sent"),
    "list_messages_all": lambda manager, session: manager.list_messages(session, "all"),
    "list_messages_undelivered": lambda manager, session: manager.list_messages(
        session, "all", undelivered_only=True
    ),
}


@pytest.mark.parametrize("read", sorted(MAILBOX_READS))
def test_mailbox_reads_do_not_scan_the_message_table(
    temp_db: HubDatabase, recipient: str, read: str
) -> None:
    """Every mailbox read the daemon polls resolves through an index, not a table scan."""
    manager = InterSessionMessageManager(temp_db)
    issued = _issued_statements(temp_db, lambda: MAILBOX_READS[read](manager, recipient))
    assert issued, read
    for statement in issued:
        scans = _scans_of(temp_db, statement, "inter_session_messages")
        assert scans, statement
        assert all("Seq Scan" not in node["Node Type"] for node in scans), (read, statement[0])


def test_mailbox_retention_finds_its_bounded_batch_through_an_index(
    temp_db: HubDatabase, recipient: str
) -> None:
    """Retention must not sort the entire delivered mailbox for each 500-row batch."""
    manager = InterSessionMessageManager(temp_db)
    with (
        temp_db.transaction() as connection,
        patch.object(temp_db, "transaction", return_value=nullcontext(connection)),
        patch.object(connection, "execute", wraps=connection.execute) as execute,
    ):
        removed = manager.delete_delivered_before(datetime.now(UTC), limit=500)
    assert removed == 500
    assert execute.call_count == 1
    call = execute.call_args
    assert call is not None
    scans = _scans_of(temp_db, (call.args[0], tuple(call.args[1])), "inter_session_messages")
    assert any(
        _leading_column(temp_db, node["Index Name"]) == "delivered_at"
        for node in scans
        if "Index Name" in node
    ), scans


@pytest.fixture
def terminal_session(
    temp_db: HubDatabase, isolated_checkout_factory: IsolatedCheckoutFactory
) -> str:
    """A machine with thousands of exited terminals and one reconcilable session."""
    project = isolated_checkout_factory(temp_db, "terminal-plan-guard").project
    session_id = str(uuid4())
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (%s, %s, %s, 'claude', %s)",
        (session_id, session_id, LOCAL_MACHINE_ID, project.id),
    )
    temp_db.execute(
        """
        INSERT INTO terminals (id, backend, ownership, state, machine_id, project_id, spawn_key)
        SELECT gen_random_uuid(), 'native', 'gobby', 'exited', %s, %s, 'plan-guard-' || g
        FROM generate_series(1, 5000) g
        """,
        (LOCAL_MACHINE_ID, project.id),
    )
    temp_db.execute(
        """
        INSERT INTO terminals
            (id, backend, ownership, state, machine_id, project_id, spawn_key, session_id)
        VALUES (%s, 'native', 'gobby', 'pending', %s, %s, %s, %s)
        """,
        (str(uuid4()), LOCAL_MACHINE_ID, project.id, session_id, session_id),
    )
    temp_db.execute("ANALYZE terminals")
    return session_id


@pytest.mark.parametrize("read", ["session", "reconciliation"])
def test_terminal_lookups_do_not_scan_settled_history(
    temp_db: HubDatabase, terminal_session: str, read: str
) -> None:
    manager = TerminalManager(temp_db)
    action = (
        (lambda: manager.list_for_session(terminal_session))
        if read == "session"
        else (lambda: manager.list_reconcilable_by_machine(LOCAL_MACHINE_ID))
    )
    issued = _issued_statements(temp_db, action)
    assert issued, read
    for statement in issued:
        scans = _scans_of(temp_db, statement, "terminals")
        assert scans, statement
        assert all("Seq Scan" not in node["Node Type"] for node in scans), (read, statement[0])
