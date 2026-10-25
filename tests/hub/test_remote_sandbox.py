from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import pytest

from gobby.hub import remote_sandbox
from gobby.hub.node_commands import CONTRACT_VERSION, ReportBody
from gobby.storage import workspace_machine_scope
from gobby.storage.agents import LocalAgentRunManager, sandbox_record
from gobby.storage.agents._sandbox_records import sandbox_list_record
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit
NODE = "6e1f9b63-2a5c-4d4a-8b5f-3c8d7a2e1b21"
OTHER = "6e1f9b63-2a5c-4d4a-8b5f-3c8d7a2e1b22"
POLICY_HASH = "cd" * 32


@dataclass(frozen=True)
class _Run:
    run_id: str
    terminal_id: str


def _seed_run(
    db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
    machine: str = NODE,
) -> _Run:
    machines = LocalMachineManager(db)
    machines.upsert_seen(NODE, TEST_USER_ID)
    machines.upsert_seen(OTHER, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: machine)
    session_id = session_manager.register_session(
        external_id=f"node-sandbox-{uuid.uuid4()}",
        machine_id=machine,
        source="claude",
        project_id=project_id,
    )
    task = LocalTaskManager(db).create_task(
        project_id=project_id,
        title="Node sandbox run",
        validation_criteria="The node run's sandbox record is kept on the hub.",
    )
    run_id, terminal_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs (
                id, machine_id, parent_session_id, child_session_id, provider, prompt, task_id
            ) VALUES (%s, %s, %s, %s, 'claude', 'work', %s)
            """,
            (run_id, machine, session_id, session_id, task.id),
        )
        conn.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id, agent_run_id
            ) VALUES (%s, 'tmux', 'gobby', 'pending', %s, %s, %s, %s)
            """,
            (terminal_id, f"gobby-{terminal_id}", machine, project_id, run_id),
        )
    return _Run(run_id=run_id, terminal_id=terminal_id)


def _sandbox(
    count: int,
    operations: dict[str, int],
    events: list[dict[str, Any]] | None = None,
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "runtime_version": "0.0.79",
        "policy_schema_version": 1,
        "policy_hash": POLICY_HASH,
        "violation_count": count,
        "violation_count_truncated": truncated,
        "denial_operations": operations,
        "recent_violations": events,
    }


def _agent(run: _Run, sandbox: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "terminal_id": run.terminal_id,
        "pid": 4242,
        "locator": "pane-1",
        "locator_key": "host-1:pane-1",
        "workspace": None,
        "pane": None,
        "sandbox": sandbox,
    }


def _exit(run: _Run, sandbox: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "terminal_id": run.terminal_id,
        "launched": True,
        "exit_code": 0,
        "reason": "exited",
        "workspace": None,
        "sandbox": sandbox,
    }


def _report(
    revision: int,
    agents: list[dict[str, Any]] | None = None,
    exits: list[dict[str, Any]] | None = None,
) -> ReportBody:
    return ReportBody.model_validate(
        {
            "v": CONTRACT_VERSION,
            "hub_epoch": None,
            "revision": revision,
            "host_epoch": 1,
            "accepted": [],
            "agents": agents or [],
            "exits": exits or [],
        }
    )


def _stored(runs: LocalAgentRunManager, run_id: str) -> dict[str, Any]:
    run = runs.get(run_id)
    assert run is not None and run.resume_metadata_json is not None
    return cast(dict[str, Any], run.resume_metadata_json["sandbox"])


def _record(stored: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "backend",
        "enforced",
        "runtime_version",
        "policy_schema_version",
        "policy_hash",
        "violation_count",
        "denial_operations",
        "recent_violations",
    )
    return {key: stored.get(key) for key in keys}


def _expected(
    count: int, operations: dict[str, int], events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "backend": "srt",
        "enforced": True,
        "runtime_version": "0.0.79",
        "policy_schema_version": 1,
        "policy_hash": POLICY_HASH,
        "violation_count": count,
        "denial_operations": operations,
        "recent_violations": events,
    }


def _updated_at(db: HubDatabase, run_id: str) -> datetime:
    row = db.fetchone("SELECT updated_at FROM agent_runs WHERE id = %s", (run_id,))
    assert row is not None
    return cast(datetime, row["updated_at"])


def test_records_merge_into_node_runs(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = sample_project["id"]
    live = _seed_run(temp_db, session_manager, project_id, monkeypatch)
    exited = _seed_run(temp_db, session_manager, project_id, monkeypatch)
    foreign = _seed_run(temp_db, session_manager, project_id, monkeypatch, machine=OTHER)
    runs = LocalAgentRunManager(temp_db)
    events = [{"line": "deny network-outbound example.com", "command": "curl example.com"}]
    live_record = _sandbox(3, {"file-write-create": 3})
    first = _report(
        1,
        [_agent(live, live_record), _agent(foreign, _sandbox(9, {"file-read-data": 9}))],
        [_exit(exited, _sandbox(2, {"network-outbound": 2}, events, truncated=True))],
    )

    remote_sandbox.apply_sandbox_records(runs, NODE, first)

    assert _record(_stored(runs, live.run_id)) == _expected(3, {"file-write-create": 3})
    exited_stored = _stored(runs, exited.run_id)
    assert _record(exited_stored) == _expected(2, {"network-outbound": 2}, events)
    assert exited_stored["violation_count_truncated"] is True
    foreign_run = runs.get(foreign.run_id)
    assert foreign_run is not None
    assert "sandbox" not in (foreign_run.resume_metadata_json or {})

    merges: list[str] = []
    merge = runs.merge_sandbox_metadata

    def counted_merge(run_id: str, updates: Mapping[str, Any], **kwargs: Any) -> object:
        merges.append(run_id)
        return merge(run_id, updates, **kwargs)

    monkeypatch.setattr(runs, "merge_sandbox_metadata", counted_merge)
    quiet_since = _updated_at(temp_db, live.run_id)
    remote_sandbox.apply_sandbox_records(runs, NODE, _report(2, [_agent(live, live_record)]))

    assert merges == []
    assert _updated_at(temp_db, live.run_id) == quiet_since

    grown = _sandbox(4, {"file-write-create": 4})
    remote_sandbox.apply_sandbox_records(runs, NODE, _report(3, [_agent(live, grown)]))

    assert merges == [live.run_id]
    assert _record(_stored(runs, live.run_id)) == _expected(4, {"file-write-create": 4})


def test_node_run_record_reads_stored_values(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _seed_run(temp_db, session_manager, sample_project["id"], monkeypatch)
    runs = LocalAgentRunManager(temp_db)
    identity = {
        "backend": "srt",
        "enforced": True,
        "runtime_version": "0.0.79",
        "policy_schema_version": 1,
        "policy_hash": POLICY_HASH,
    }
    # The stored shape a node report merges: no violation_path, no retained log.
    runs.merge_sandbox_metadata(
        run.run_id,
        {
            **identity,
            "violation_count": 7,
            "denial_operations": {"file-write-create": 7},
            "report_revision": 1,
        },
    )
    active = runs.get(run.run_id)
    assert active is not None and active.resume_metadata_json is not None

    active_record = sandbox_record(active.resume_metadata_json, include_events=True)
    active_list = sandbox_list_record(active.resume_metadata_json["sandbox"], active=True)

    assert active_record is not None and active_list is not None
    assert active_record["violation_count"] == 7
    assert active_record["violations"] == []
    assert active_list["violation_count"] == 7

    events: list[dict[str, Any]] = [
        {"line": f"deny file-read-data /node/{index}", "command": f"cat /node/{index}"}
        for index in range(104)
    ]
    long_command = "x" * 3000
    events.append({"line": "deny file-write-create /node/long", "command": long_command})
    runs.merge_sandbox_metadata(
        run.run_id,
        {
            "violation_count": 105,
            "denial_operations": {"file-read-data": 104, "file-write-create": 1},
            "recent_violations": events,
            "report_revision": 2,
            "report_final": True,
        },
    )
    assert runs.complete(run.run_id) is not None
    ended = runs.get(run.run_id)
    assert ended is not None and ended.resume_metadata_json is not None

    ended_record = sandbox_record(ended.resume_metadata_json, include_events=True)
    ended_list = sandbox_list_record(ended.resume_metadata_json["sandbox"], active=False)

    assert ended_record is not None and ended_list is not None
    assert ended_record["violation_count"] == 105
    assert ended_list["violation_count"] == 105
    violations = ended_record["violations"]
    assert len(violations) == 100
    assert violations[:-1] == events[5:-1]
    assert violations[-1] == {
        "line": "deny file-write-create /node/long",
        "command": long_command[:2048],
        "command_length": 3000,
        "command_truncated": True,
    }
    assert "report_revision" not in ended_record
    assert "report_final" not in ended_record
    assert "report_revision" not in ended_list
    assert "report_final" not in ended_list


def test_older_report_never_overwrites_newer(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _seed_run(temp_db, session_manager, sample_project["id"], monkeypatch)
    runs = LocalAgentRunManager(temp_db)
    newer_operations = {"file-write-create": 4, "network-outbound": 1}
    r1 = _report(1, [_agent(run, _sandbox(2, {"file-write-create": 2}))])
    r2 = _report(2, [_agent(run, _sandbox(5, newer_operations))])

    remote_sandbox.apply_sandbox_records(runs, NODE, r2)
    merged_at = _updated_at(temp_db, run.run_id)
    remote_sandbox.apply_sandbox_records(runs, NODE, r1)

    assert _updated_at(temp_db, run.run_id) == merged_at
    assert _record(_stored(runs, run.run_id)) == _expected(5, newer_operations)


def test_exit_record_is_final(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _seed_run(temp_db, session_manager, sample_project["id"], monkeypatch)
    runs = LocalAgentRunManager(temp_db)
    operations = {"file-write-unlink": 4}
    events = [{"line": "deny file-write-unlink /node/a", "command": "rm /node/a"}]
    exit_entry = _exit(run, _sandbox(4, operations, events))
    later = _sandbox(9, {"file-read-data": 9})

    remote_sandbox.apply_sandbox_records(runs, NODE, _report(2, [_agent(run, _sandbox(1, {}))]))
    remote_sandbox.apply_sandbox_records(runs, NODE, _report(3, exits=[exit_entry]))
    final_at = _updated_at(temp_db, run.run_id)
    final = _expected(4, operations, events)
    assert _record(_stored(runs, run.run_id)) == final

    for report in (
        _report(1, [_agent(run, later)]),
        _report(4, [_agent(run, later)]),
        _report(5, exits=[exit_entry]),
    ):
        remote_sandbox.apply_sandbox_records(runs, NODE, report)

        assert _updated_at(temp_db, run.run_id) == final_at
        assert _record(_stored(runs, run.run_id)) == final
