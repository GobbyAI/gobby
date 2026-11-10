"""Roster assembly must skip enrichment of unassigned, ended history."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.attention import AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id


def _plan_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from _plan_nodes(child)


@pytest.mark.parametrize("include_runs", [False, True], ids=["sessions", "runs-and-sessions"])
def test_roster_does_not_probe_claims_for_unassigned_ended_history(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    include_runs: bool,
) -> None:
    live = session_manager.register(
        external_id="history-roster-live",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, status, project_id)
        SELECT gen_random_uuid(), 'ended-history-' || n::text, %s, 'codex', 'expired', %s
        FROM generate_series(1, 1000) n
        """,
        (live.machine_id, sample_project["id"]),
    )
    if include_runs:
        temp_db.execute(
            """
            INSERT INTO agent_runs
                (id, machine_id, parent_session_id, child_session_id, provider, prompt, status)
            SELECT gen_random_uuid(), machine_id, %s, id, 'codex', 'ended history', 'success'
            FROM sessions WHERE external_id LIKE 'ended-history-%%'
            """,
            (live.id,),
        )
        temp_db.execute("ANALYZE agent_runs")
    temp_db.execute("ANALYZE sessions")
    manager = AttentionStateManager(temp_db, epoch="history-roster")
    with patch.object(temp_db, "fetchall", wraps=temp_db.fetchall) as fetchall:
        rows = manager.load_roster_rows(live_session_statuses=["active"])
    assert [row.source_id for row in rows] == [live.id]
    query, params = fetchall.call_args.args
    explained = temp_db.fetchall("EXPLAIN (ANALYZE, FORMAT JSON) " + query, params)
    raw = explained[0]["QUERY PLAN"]
    plan = json.loads(raw) if isinstance(raw, str) else raw
    claim_probes = sum(
        node["Actual Loops"]
        for node in _plan_nodes(plan[0]["Plan"])
        if node.get("Relation Name") == "tasks"
        and node.get("Index Name") == "idx_tasks_claimed_session"
    )
    assert claim_probes <= 1, f"roster probed claims {claim_probes} times for one live seat"
