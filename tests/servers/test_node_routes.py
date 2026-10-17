from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.node_reports import NodeReportsConfig
from gobby.hub import node_reports
from gobby.hub.node_commands import ReportBody
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.servers.routes import nodes
from gobby.storage import workspace_machine_scope
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager
from gobby.storage.worktrees import LocalWorktreeManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit
NODE = "5d0e8a52-1f4b-4c39-9a4e-2b7c6f1d0a11"
OTHER = "5d0e8a52-1f4b-4c39-9a4e-2b7c6f1d0a12"
LOCAL = "5d0e8a52-1f4b-4c39-9a4e-2b7c6f1d0a13"
SECRET = "node-routes-secret"


@dataclass(frozen=True)
class _Run:
    run_id: str
    terminal_id: str
    task_id: str
    session_id: str


def _headers(machine: str = NODE) -> dict[str, str]:
    return {
        "X-Gobby-Front-Door": SECRET,
        "X-Gobby-User-Id": TEST_USER_ID,
        "X-Gobby-Machine-Id": machine,
        "X-Gobby-Key-Id": "node-key",
    }


async def _run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(func, *args, **kwargs)


def _client(
    db: HubDatabase, monkeypatch: pytest.MonkeyPatch, config: object | None = None
) -> TestClient:
    node_reports.node_report_inbox.cache_clear()
    auth = AuthService(lambda: db)
    auth.bind_runtime(
        grant_service=None, local_machine_id=LOCAL, clock=None, front_door_secret=SECRET
    )
    server = cast(
        HTTPServer,
        SimpleNamespace(
            auth_service=auth,
            services=SimpleNamespace(database=db),
            run_db=_run_db,
            config=config,
        ),
    )
    app = FastAPI()
    monkeypatch.setattr(nodes, "require_machine_id", lambda: LOCAL)
    app.include_router(nodes.create_nodes_router(server))
    return TestClient(app)


def _machines(db: HubDatabase) -> None:
    machines = LocalMachineManager(db)
    machines.upsert_seen(NODE, TEST_USER_ID)
    machines.upsert_seen(OTHER, TEST_USER_ID)


def _workspace(
    path: str, mode: str = "worktree", branch: str = "lane-node", base: str | None = "0.5.0"
) -> dict[str, Any]:
    return {"mode": mode, "path": path, "branch_name": branch, "base_branch": base}


def _agent(run_id: str, terminal_id: str, workspace: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "terminal_id": terminal_id,
        "pid": 4242,
        "locator": "pane-1",
        "locator_key": "host-1:pane-1",
        "workspace": workspace,
        "pane": None,
        "sandbox": None,
    }


def _exit(run_id: str, terminal_id: str, workspace: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "terminal_id": terminal_id,
        "launched": True,
        "exit_code": 0,
        "reason": "exited",
        "workspace": workspace,
        "sandbox": None,
    }


def _fresh_exit() -> dict[str, Any]:
    return _exit(str(uuid.uuid4()), str(uuid.uuid4()))


def _report(
    epoch: int | None,
    revision: int,
    agents: list[dict[str, Any]] | None = None,
    exits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "v": 3,
        "hub_epoch": epoch,
        "revision": revision,
        "host_epoch": 1,
        "accepted": [],
        "agents": agents or [],
        "exits": exits or [],
    }


def _seed_run(
    db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    terminal_machine: str = NODE,
) -> _Run:
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: NODE)
    session_id = session_manager.register_session(
        external_id=f"node-run-{uuid.uuid4()}",
        machine_id=NODE,
        source="claude",
        project_id=project_id,
    )
    task = LocalTaskManager(db).create_task(
        project_id=project_id,
        title="Remote run",
        validation_criteria="The remote run's workspace is recorded.",
    )
    run_id = str(uuid.uuid4())
    terminal_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO agent_runs (
                id, machine_id, parent_session_id, child_session_id, provider, prompt, task_id
            ) VALUES (%s, %s, %s, %s, 'claude', 'work', %s)
            """,
            (run_id, NODE, session_id, session_id, task.id),
        )
        conn.execute(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id, agent_run_id
            ) VALUES (%s, 'tmux', 'gobby', 'pending', %s, %s, %s, %s)
            """,
            (terminal_id, f"gobby-{terminal_id}", terminal_machine, project_id, run_id),
        )
    return _Run(run_id=run_id, terminal_id=terminal_id, task_id=task.id, session_id=session_id)


def _workspace_rows(db: HubDatabase, table: str) -> list[dict[str, Any]]:
    path_column = "worktree_path" if table == "worktrees" else "clone_path"
    rows = db.fetchall(
        f"SELECT *, {path_column} AS path FROM {table} WHERE machine_id = %s",  # nosec B608
        (NODE,),
    )
    return [
        {key: str(value) if isinstance(value, uuid.UUID) else value for key, value in row.items()}
        for row in rows
    ]


def _updated_at(db: HubDatabase, path: str) -> datetime:
    row = db.fetchone(
        "SELECT updated_at FROM worktrees WHERE machine_id = %s AND worktree_path = %s",
        (NODE, path),
    )
    assert row is not None
    return cast(datetime, row["updated_at"])


def _post(
    client: TestClient, route: str, body: dict[str, Any], machine: str = NODE
) -> tuple[int, Any]:
    response = client.post(f"/api/nodes/{route}", headers=_headers(machine), json=body)
    return response.status_code, response.json()


def _admit(inbox: node_reports.NodeReportInbox, report: ReportBody) -> bool:
    admission = inbox.admit(
        NODE,
        inbox.hub_epoch,
        report.revision,
        node_reports.report_size(report),
        NodeReportsConfig().max_pending_bytes_per_machine,
    )
    return admission is node_reports.Admission.ADMITTED


def _put(inbox: node_reports.NodeReportInbox, report: ReportBody) -> None:
    inbox.put(NODE, report, node_reports.report_size(report))


def _compact_size(body: dict[str, Any]) -> int:
    return len(json.dumps(body, separators=(",", ":")).encode())


def test_checkout_registration_uses_key_machine(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _machines(temp_db)
    root = f"/node-only/checkouts/{uuid.uuid4()}"
    body = {"v": 3, "project_id": sample_project["id"], "root_path": root}

    with _client(temp_db, monkeypatch) as client:
        first = _post(client, "checkouts", body)
        repeat = _post(client, "checkouts", body)

    assert first == (200, {"root_path": root, "created": True})
    assert repeat == (200, {"root_path": root, "created": False})
    checkout = LocalProjectCheckoutManager(temp_db).get(NODE, sample_project["id"])
    assert checkout is not None
    assert checkout.root_path == root
    assert not os.path.exists(root)


def test_checkout_registration_refusals(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machines(temp_db)
    checkouts = LocalProjectCheckoutManager(temp_db)
    project_id = sample_project["id"]
    held = f"/node-only/held/{uuid.uuid4()}"
    other_project = project_manager.create(f"node-other-{uuid.uuid4()}")
    checkouts.register(NODE, other_project.id, held)
    root = f"/node-only/root/{uuid.uuid4()}"

    def body(project: str, root_path: str) -> dict[str, Any]:
        return {"v": 3, "project_id": project, "root_path": root_path}

    with _client(temp_db, monkeypatch) as client:
        unknown = _post(client, "checkouts", body(str(uuid.uuid4()), root))
        relative = _post(client, "checkouts", body(project_id, "relative/root"))
        taken = _post(client, "checkouts", body(project_id, held))
        assert checkouts.get(NODE, project_id) is None
        created = _post(client, "checkouts", body(project_id, root))
        conflict = _post(client, "checkouts", body(project_id, f"{root}-elsewhere"))
        kept = sorted(c.root_path for c in checkouts.list_for_machine(NODE))
        assert project_manager.soft_delete(project_id)
        deleted = _post(client, "checkouts", body(project_id, root))

    assert unknown == (404, {"detail": "project_not_found"})
    assert relative == (422, {"detail": "invalid_body"})
    assert taken == (409, {"detail": "checkout_root_taken"})
    assert created == (200, {"root_path": root, "created": True})
    assert conflict == (409, {"detail": "checkout_conflict"})
    assert kept == sorted([held, root])
    assert deleted == (409, {"detail": "project_deleted"})
    assert [c.root_path for c in checkouts.list_for_machine(NODE)] == [held]


def test_report_inbox_replaces_snapshot_and_merges_exits(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machines(temp_db)
    first_exit = _fresh_exit()
    second_exit = _fresh_exit()
    agent = _agent(str(uuid.uuid4()), str(uuid.uuid4()), None)
    many = [_fresh_exit() for _ in range(1000)]

    with _client(temp_db, monkeypatch) as client:
        inbox = node_reports.node_report_inbox()
        epoch = inbox.hub_epoch
        first = _post(client, "report", _report(epoch, 1, exits=[first_exit]))
        second = _post(client, "report", _report(epoch, 2, [agent], [second_exit]))
        taken = inbox.take()
        drained = inbox.take()
        bulk = _post(client, "report", _report(epoch, 3, exits=many))
        last = _post(client, "report", _report(epoch, 4, exits=[_fresh_exit()]))
        later = inbox.take()

    assert (first, second, bulk, last) == ((200, {}), (200, {}), (200, {}), (200, {}))
    assert list(taken) == [NODE]
    assert taken[NODE].snapshot.revision == 2
    assert [a.run_id for a in taken[NODE].snapshot.agents] == [agent["run_id"]]
    assert {e.run_id for e in taken[NODE].exits} == {first_exit["run_id"], second_exit["run_id"]}
    assert drained == {}
    assert later[NODE].snapshot.revision == 4
    assert len(later[NODE].exits) == 1001
    assert inbox.take() == {}


@pytest.mark.parametrize("route", ["checkouts", "report"])
@pytest.mark.parametrize(
    ("proof", "status", "code"),
    [
        ("missing", 403, "node_identity_required"),
        ("forged", 403, "node_identity_required"),
        ("local", 403, "machine_is_local"),
        ("machine_id", 422, "invalid_body"),
        ("version", 422, "unsupported_version"),
    ],
)
def test_node_routes_refuse_unverified_and_local_identity(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    proof: str,
    status: int,
    code: str,
) -> None:
    _machines(temp_db)
    headers = {
        "missing": {},
        "forged": _headers() | {"X-Gobby-Front-Door": "forged"},
        "local": _headers(LOCAL),
    }.get(proof, _headers())
    extras: dict[str, dict[str, Any]] = {"machine_id": {"machine_id": NODE}, "version": {"v": 3}}
    extra = extras.get(proof, {})

    with _client(temp_db, monkeypatch) as client:
        inbox = node_reports.node_report_inbox()
        body: dict[str, Any] = (
            {"v": 3, "project_id": sample_project["id"], "root_path": "/node-only/refused"}
            if route == "checkouts"
            else _report(inbox.hub_epoch, 1, exits=[_fresh_exit()])
        )
        response = client.post(f"/api/nodes/{route}", headers=headers, json=body | extra)
        taken = inbox.take()
        admitted = _admit(inbox, ReportBody.model_validate(_report(None, 1)))

    assert (response.status_code, response.json()) == (status, {"detail": code})
    assert taken == {}
    assert admitted
    checkouts = LocalProjectCheckoutManager(temp_db)
    assert checkouts.list_for_machine(NODE) == []
    assert checkouts.list_for_machine(LOCAL) == []


def test_report_order_and_epoch_barrier(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machines(temp_db)
    run = _seed_run(temp_db, session_manager, sample_project["id"], monkeypatch)
    path = f"/node-only/worktrees/{uuid.uuid4()}"
    agents = [_agent(run.run_id, run.terminal_id, _workspace(path))]

    with _client(temp_db, monkeypatch) as client:
        old = node_reports.node_report_inbox()
        null_epoch = _post(client, "report", _report(None, 5, agents))
        node_reports.node_report_inbox.cache_clear()
        inbox = node_reports.node_report_inbox()
        restarted = _post(client, "report", _report(old.hub_epoch, 5, agents))
        admitted = _post(client, "report", _report(inbox.hub_epoch, 5))
        duplicate = _post(client, "report", _report(inbox.hub_epoch, 5, agents))
        out_of_order = _post(client, "report", _report(inbox.hub_epoch, 4, agents))
        taken = inbox.take()

    stale = {"code": "stale_report", "hub_epoch": inbox.hub_epoch}
    assert inbox.hub_epoch != old.hub_epoch
    assert null_epoch == (409, {"code": "stale_report", "hub_epoch": old.hub_epoch})
    assert restarted == (409, stale)
    assert admitted == (200, {})
    assert duplicate == (409, stale)
    assert out_of_order == (409, stale)
    assert _workspace_rows(temp_db, "worktrees") == []
    assert taken[NODE].snapshot.revision == 5
    assert taken[NODE].snapshot.agents == []

    def body(revision: int, exits: list[dict[str, Any]] | None = None) -> ReportBody:
        return ReportBody.model_validate(_report(None, revision, exits=exits))

    finishing = node_reports.NodeReportInbox()
    assert _admit(finishing, body(1))
    assert _admit(finishing, body(2))
    _put(finishing, body(2))
    _put(finishing, body(1))
    assert finishing.take()[NODE].snapshot.revision == 2

    intervening = node_reports.NodeReportInbox()
    r1 = body(1, [_fresh_exit()])
    assert _admit(intervening, r1)
    assert _admit(intervening, body(2))
    _put(intervening, body(2))
    assert intervening.take()[NODE].snapshot.revision == 2
    _put(intervening, r1)
    assert intervening.take() == {}
    assert _admit(intervening, body(3))
    _put(intervening, body(3))
    resumed = intervening.take()
    assert resumed[NODE].snapshot.revision == 3
    assert [e.run_id for e in resumed[NODE].exits] == [r1.exits[0].run_id]


def test_reported_workspace_is_durable_before_200(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machines(temp_db)
    project_id = sample_project["id"]
    live = _seed_run(temp_db, session_manager, project_id, monkeypatch)
    exited = _seed_run(temp_db, session_manager, project_id, monkeypatch)
    foreign = _seed_run(temp_db, session_manager, project_id, monkeypatch, terminal_machine=OTHER)
    live_path = f"/node-only/worktrees/{uuid.uuid4()}"
    clone_path = f"/node-only/clones/{uuid.uuid4()}"
    agents = [
        _agent(live.run_id, live.terminal_id, _workspace(live_path)),
        _agent(foreign.run_id, foreign.terminal_id, _workspace(f"/node-only/f/{uuid.uuid4()}")),
        _agent(exited.run_id, live.terminal_id, _workspace(f"/node-only/m/{uuid.uuid4()}")),
    ]
    clone = _workspace(clone_path, "clone", "lane-clone", None)
    exits = [_exit(exited.run_id, exited.terminal_id, clone)]

    with _client(temp_db, monkeypatch) as client:
        epoch = node_reports.node_report_inbox().hub_epoch
        first = _post(client, "report", _report(epoch, 1, agents, exits))
        worktrees = _workspace_rows(temp_db, "worktrees")
        clones = _workspace_rows(temp_db, "clones")
        repeat = _post(client, "report", _report(epoch, 2, agents, exits))
        node_reports.node_report_inbox.cache_clear()

    assert (first, repeat) == ((200, {}), (200, {}))
    assert [
        (r["path"], r["branch_name"], r["base_branch"], r["project_id"], r["status"])
        for r in worktrees
    ] == [(live_path, "lane-node", "0.5.0", project_id, "active")]
    assert (worktrees[0]["task_id"], worktrees[0]["agent_session_id"]) == (
        live.task_id,
        live.session_id,
    )
    assert [(r["path"], r["branch_name"], r["base_branch"]) for r in clones] == [
        (clone_path, "lane-clone", None)
    ]
    assert (clones[0]["task_id"], clones[0]["agent_session_id"]) == (
        exited.task_id,
        exited.session_id,
    )
    assert [r["id"] for r in _workspace_rows(temp_db, "worktrees")] == [worktrees[0]["id"]]
    assert [r["id"] for r in _workspace_rows(temp_db, "clones")] == [clones[0]["id"]]
    artifacts = TaskArtifactManager(temp_db)
    assert artifacts.get_artifacts(live.task_id).worktree_path is None
    assert artifacts.get_artifacts(exited.task_id).clone_path is None
    hub_rows = LocalWorktreeManager(temp_db).list_worktrees(limit=None)
    assert live_path not in {w.worktree_path for w in hub_rows}


def test_reported_workspace_moves_updated_at(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machines(temp_db)
    project_id = sample_project["id"]
    live = _seed_run(temp_db, session_manager, project_id, monkeypatch)
    exited = _seed_run(temp_db, session_manager, project_id, monkeypatch)
    live_path = f"/node-only/worktrees/{uuid.uuid4()}"
    exited_path = f"/node-only/worktrees/{uuid.uuid4()}"
    agents = [_agent(live.run_id, live.terminal_id, _workspace(live_path, branch="lane-live"))]
    exits = [_exit(exited.run_id, exited.terminal_id, _workspace(exited_path, branch="lane-exit"))]

    with _client(temp_db, monkeypatch) as client:
        epoch = node_reports.node_report_inbox().hub_epoch
        first = _post(client, "report", _report(epoch, 1, agents))
        recorded = _updated_at(temp_db, live_path)
        again = _post(client, "report", _report(epoch, 2, agents))
        moved = _updated_at(temp_db, live_path)
        rows_after_repeat = len(_workspace_rows(temp_db, "worktrees"))
        mixed = _post(client, "report", _report(epoch, 3, agents, exits))

    assert (first, again, mixed) == ((200, {}), (200, {}), (200, {}))
    assert moved > recorded
    assert rows_after_repeat == 1
    assert _updated_at(temp_db, live_path) > _updated_at(temp_db, exited_path)
    assert len(_workspace_rows(temp_db, "worktrees")) == 2


def test_report_byte_budgets(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machines(temp_db)
    run = _seed_run(temp_db, session_manager, sample_project["id"], monkeypatch)
    path = f"/node-only/worktrees/{uuid.uuid4()}"
    agent = _agent(run.run_id, run.terminal_id, _workspace(path))
    kept = [_fresh_exit() for _ in range(10)]
    bulk = [_fresh_exit() for _ in range(30)]
    repeated_exit = _fresh_exit()
    config = SimpleNamespace(node_reports=NodeReportsConfig(max_request_bytes=2048))

    def failed_write(*_args: object) -> None:
        raise RuntimeError("workspace write failed")

    with _client(temp_db, monkeypatch, config) as client:
        inbox = node_reports.node_report_inbox()
        epoch = inbox.hub_epoch
        too_large = _post(client, "report", _report(epoch, 1, [agent], bulk))
        unparsed = client.post("/api/nodes/report", headers=_headers(), content=b"{" * 4096)
        after_too_large = inbox.take()

        first = _report(epoch, 1, exits=kept)
        refused = _report(epoch, 2, [agent], bulk)
        budget = _compact_size(refused) + _compact_size(first) // 2
        config.node_reports = NodeReportsConfig(max_pending_bytes_per_machine=budget)
        accepted = _post(client, "report", first)
        backlog = client.post("/api/nodes/report", headers=_headers(), json=refused)
        rows_while_full = _workspace_rows(temp_db, "worktrees")
        other = _post(client, "report", _report(epoch, 1, exits=bulk), machine=OTHER)
        held = inbox.take()
        retried = _post(client, "report", refused)
        rows_after_retry = _workspace_rows(temp_db, "worktrees")
        released = inbox.take()

        monkeypatch.setattr(nodes, "record_remote_workspaces", failed_write)
        with pytest.raises(RuntimeError, match="workspace write failed"):
            _post(client, "report", _report(epoch, 3, [agent], bulk))
        monkeypatch.setattr(
            nodes, "record_remote_workspaces", node_reports.record_remote_workspaces
        )
        after_failure = _post(client, "report", _report(epoch, 4, [agent], bulk))
        inbox.take()

        small = _report(epoch, 5, exits=[repeated_exit])
        config.node_reports = NodeReportsConfig(
            max_pending_bytes_per_machine=3 * _compact_size(small)
        )
        steady = [
            _post(client, "report", _report(epoch, revision, exits=[repeated_exit]))
            for revision in range(5, 25)
        ]
        last = inbox.take()

    assert too_large == (413, {"detail": "report_too_large"})
    assert (unparsed.status_code, unparsed.json()) == (413, {"detail": "report_too_large"})
    assert after_too_large == {}
    assert accepted == (200, {})
    assert (backlog.status_code, backlog.json()) == (429, {"detail": "report_backlog_full"})
    assert int(backlog.headers["Retry-After"]) > 0
    assert rows_while_full == []
    assert other == (200, {})
    assert set(held) == {NODE, OTHER}
    assert held[NODE].snapshot.revision == 1
    assert {e.run_id for e in held[NODE].exits} == {e["run_id"] for e in kept}
    assert retried == (200, {})
    assert [r["path"] for r in rows_after_retry] == [path]
    assert released[NODE].snapshot.revision == 2
    assert {e.run_id for e in released[NODE].exits} == {e["run_id"] for e in bulk}
    assert after_failure == (200, {})
    assert steady == [(200, {})] * 20
    assert last[NODE].snapshot.revision == 24
    assert [e.run_id for e in last[NODE].exits] == [repeated_exit["run_id"]]
    assert DaemonConfig().node_reports == NodeReportsConfig(
        max_request_bytes=8 * 1024 * 1024, max_pending_bytes_per_machine=16 * 1024 * 1024
    )


def test_checkout_body_is_capped_before_parse_or_write(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machines(temp_db)
    checkouts = LocalProjectCheckoutManager(temp_db)
    root = f"/node-only/capped/{uuid.uuid4()}"
    config = SimpleNamespace(node_reports=NodeReportsConfig(max_request_bytes=2048))
    oversized = {
        "v": 3,
        "project_id": sample_project["id"],
        "root_path": root,
        "padding": "x" * 4096,
    }

    def malformed() -> Iterator[bytes]:
        yield b"{" * 4096

    with _client(temp_db, monkeypatch, config) as client:
        valid_too_large = _post(client, "checkouts", oversized)
        chunked = client.post("/api/nodes/checkouts", headers=_headers(), content=malformed())

    assert valid_too_large == (413, {"detail": "checkout_too_large"})
    assert (chunked.status_code, chunked.json()) == (413, {"detail": "checkout_too_large"})
    assert checkouts.list_for_machine(NODE) == []


def test_out_of_order_duplicate_exit_is_charged_once() -> None:
    run_id, terminal_id = str(uuid.uuid4()), str(uuid.uuid4())
    large = _exit(run_id, terminal_id, _workspace("/node-only/" + "p" * 4096))
    newer = ReportBody.model_validate(_report(None, 2, exits=[large]))
    older = ReportBody.model_validate(_report(None, 1, exits=[_exit(run_id, terminal_id)]))
    small = older.exits[0]
    # Retained bytes: the newest snapshot without its exits, plus each gathered exit once.
    real = node_reports.report_size(newer.model_copy(update={"exits": []}))
    real += node_reports.report_size(small)
    probe = node_reports.report_size(ReportBody.model_validate(_report(None, 3)))

    inbox = node_reports.NodeReportInbox()
    assert _admit(inbox, older)
    assert _admit(inbox, newer)
    _put(inbox, newer)
    _put(inbox, older)
    refused = inbox.admit(NODE, inbox.hub_epoch, 3, probe, real + probe - 1)
    assert refused is node_reports.Admission.BACKLOG_FULL
    assert inbox.admit(NODE, inbox.hub_epoch, 3, probe, real + probe) is (
        node_reports.Admission.ADMITTED
    )
    inbox.release(NODE, probe)

    taken = inbox.take()[NODE]
    retained = node_reports.report_size(taken.snapshot)
    retained += sum(node_reports.report_size(entry) for entry in taken.exits)
    assert not taken.snapshot.exits
    assert retained == real
    assert inbox.admit(NODE, inbox.hub_epoch, 4, probe, probe) is node_reports.Admission.ADMITTED
