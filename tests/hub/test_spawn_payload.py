"""Contract tests for the hub spawn payload builder (node-halves 3.2)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.agents.reasoning import SpawnReasoningResolution
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_executor_providers import _agent_prompt_prefix
from gobby.agents.spawn_models import SpawnRequest
from gobby.config.tmux import TmuxConfig
from gobby.hub.node_commands import SpawnMonitor, SpawnPayload
from gobby.hub.spawn_payload import build_spawn_payload
from gobby.mcp_proxy.tools.spawn_agent._generation_endpoint import SpawnGenerationEndpointResolution
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import make_agent_definition
from tests.fixtures.isolated_checkout import insert_isolated_machine

pytestmark = pytest.mark.integration

_PROJECT_ID = "55555555-5555-4555-8555-555555555555"
_SOURCE_ROOT = "/srv/gobby/project"
_TERMINAL_ID = "66666666-6666-4666-8666-666666666666"
_NATIVE_ID = "019a1b2c-3d4e-7f00-8a9b-0c1d2e3f4a5b"
_TOKEN = "tok-spawn-payload-secret"
_PERSONA = "You are the developer agent."


def _run(
    *,
    machine_id: str | None = None,
    session_id: str | None = None,
    provider: str = "claude",
    task_id: str | None = None,
) -> AgentRun:
    now = datetime.now(UTC)
    return AgentRun(
        id=str(uuid.uuid4()),
        parent_session_id=str(uuid.uuid4()),
        machine_id=machine_id or require_machine_id(),
        provider=provider,
        prompt="implement the thing",
        status="pending",
        created_at=now,
        updated_at=now,
        child_session_id=session_id or str(uuid.uuid4()),
        workflow_name="developer",
        task_id=task_id,
    )


def _request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "prompt": "implement the thing",
        "project_id": _PROJECT_ID,
        "project_path": _SOURCE_ROOT,
        "terminal_id": _TERMINAL_ID,
        "agent_depth": 1,
        "max_agent_depth": 5,
        "title": "lane 5",
        "timeout": None,
        "droid_mode": "exec",
        "initial_variables": {},
        "agent_lookup_name": None,
        "agent_body": None,
        "checkout_mode": None,
        "branch_name": None,
        "base_branch": None,
    }
    request.update(overrides)
    return request


def _endpoint(**overrides: Any) -> SpawnGenerationEndpointResolution:
    fields: dict[str, Any] = {
        "model": "gpt-test",
        "api_base": None,
        "api_token": None,
        "is_local": True,
    }
    fields.update(overrides)
    return SpawnGenerationEndpointResolution(**fields)


def _reasoning() -> SpawnReasoningResolution:
    return SpawnReasoningResolution(
        requested_effort="medium",
        effective_effort="medium",
        reasoning_required=False,
        status="not_requested",
    )


def _sandbox(**overrides: Any) -> SandboxConfig:
    fields: dict[str, Any] = {"enabled": True, "backend": "srt", "mode": "restrictive"}
    fields.update(overrides)
    return SandboxConfig(**fields)


def _tmux() -> TmuxConfig:
    return TmuxConfig(
        auto_enter_agent_terminals=True,
        auto_enter_agent_interval_seconds=30,
        auto_enter_approval_prompts=True,
        init_timeout_seconds=120,
    )


def _build(
    db: HubDatabase,
    run: AgentRun,
    request: Mapping[str, Any],
    *,
    endpoint: SpawnGenerationEndpointResolution | None = None,
    sandbox: SandboxConfig | None = None,
    resume_metadata: Mapping[str, Any] | None = None,
    code_index_mode: str | None = "managed",
    tmux: TmuxConfig | None = None,
) -> SpawnPayload:
    return build_spawn_payload(
        db,
        run=run,
        request=request,
        endpoint=endpoint or _endpoint(),
        reasoning=_reasoning(),
        sandbox_config=sandbox or _sandbox(),
        resume_metadata=resume_metadata or {},
        code_index_mode=code_index_mode,
        tmux_config=tmux or _tmux(),
    )


def _insert_session(db: HubDatabase, project_id: str, machine_id: str) -> str:
    session_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, %s, 'codex', %s)
        """,
        (session_id, f"spawn-payload-{session_id}", machine_id, project_id),
    )
    return session_id


def _row(
    kind: str,
    *,
    session: str,
    machine: str,
    path: str,
    branch: str,
    updated: int,
    created: int = 0,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "session": session,
        "machine": machine,
        "path": path,
        "branch": branch,
        "updated": updated,
        "created": created,
        "status": status,
    }


def _seed_workspaces(db: HubDatabase, project_id: str, rows: list[dict[str, Any]]) -> None:
    db.execute("DELETE FROM worktrees")
    db.execute("DELETE FROM clones")
    base = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    for row in rows:
        created_at = base + timedelta(minutes=row["created"])
        updated_at = base + timedelta(minutes=row["updated"])
        values = (
            str(uuid.uuid4()),
            project_id,
            row["machine"],
            row["branch"],
            row["path"],
            row["session"],
            row["status"],
            created_at,
            updated_at,
        )
        if row["kind"] == "worktree":
            db.execute(
                """
                INSERT INTO worktrees (id, project_id, machine_id, branch_name, worktree_path,
                    base_branch, agent_session_id, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, '0.5.0', %s, %s, %s, %s)
                """,
                values,
            )
        else:
            db.execute(
                """
                INSERT INTO clones (id, project_id, machine_id, branch_name, clone_path,
                    base_branch, agent_session_id, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, '0.5.0', %s, %s, %s, %s)
                """,
                values,
            )


def test_sandbox_is_resolved_config(temp_db: HubDatabase) -> None:
    sandbox = _sandbox(
        allow_network=False,
        extra_read_paths=["/opt/shared"],
        allowed_domains=["github.com"],
        allow_git_network=True,
    )

    payload = _build(temp_db, _run(), _request(), sandbox=sandbox)

    wire = payload.model_dump(mode="json")
    assert wire["sandbox"] == sandbox.model_dump(mode="json")
    assert "network" not in wire["sandbox"]


def test_endpoint_fields(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
    enable_log_propagation: None,
) -> None:
    caplog.set_level(logging.DEBUG)
    endpoint = _endpoint(
        model="gpt-oss-20b",
        api_base="http://127.0.0.1:1234/v1",
        api_token=_TOKEN,
        codex_oss_provider="lmstudio",
        codex_config_overrides=("model_reasoning_effort=high",),
        child_env={"GOBBY_LANE": "5"},
    )

    payload = _build(temp_db, _run(), _request(), endpoint=endpoint)

    assert payload.model == "gpt-oss-20b"
    assert payload.api_base == "http://127.0.0.1:1234/v1"
    assert payload.api_token == _TOKEN
    assert payload.codex_oss_provider == "lmstudio"
    assert payload.codex_config_overrides == ["model_reasoning_effort=high"]
    assert payload.extra_env == {"GOBBY_LANE": "5"}
    assert _build(temp_db, _run(), _request(), endpoint=_endpoint()).extra_env == {}
    assert _TOKEN not in caplog.text


def test_resume_fields(temp_db: HubDatabase) -> None:
    run = _run()

    resumed = _build(
        temp_db,
        run,
        _request(),
        resume_metadata={
            "resume_existing_session": True,
            "provider_native_session_id": _NATIVE_ID,
            "execution_mode": "interactive",
        },
    )
    fresh = _build(
        temp_db,
        run,
        _request(),
        resume_metadata={
            "resume_existing_session": False,
            "provider_native_session_id": _NATIVE_ID,
            "execution_mode": "one_shot",
        },
    )

    assert resumed.resume_session_id == _NATIVE_ID
    assert resumed.execution_mode == "interactive"
    assert fresh.resume_session_id is None
    assert fresh.execution_mode == "one_shot"
    assert run.child_session_id not in {resumed.resume_session_id, fresh.resume_session_id}


def test_codex_persona_prefix(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = sample_project["id"]
    machine_id = require_machine_id()
    body = make_agent_definition(name="developer", prompts={"agent": _PERSONA})
    session_id = _insert_session(temp_db, project_id, machine_id)
    codex_run = _run(machine_id=machine_id, session_id=session_id, provider="codex")

    def _resolve(name: str, db: object, *, project_id: str) -> object:
        return body

    monkeypatch.setattr("gobby.workflows.agent_resolver.resolve_agent", _resolve)
    local = SpawnRequest(
        prompt="implement the thing",
        cwd=_SOURCE_ROOT,
        provider="codex",
        session_id=session_id,
        run_id=codex_run.id,
        parent_session_id=codex_run.parent_session_id,
        project_id=project_id,
        agent_name="developer",
        prepared_spawn=cast(Any, None),
        session_manager=cast(Any, SimpleNamespace(_storage=SimpleNamespace(db=temp_db))),
    )
    assert _agent_prompt_prefix(local) == _PERSONA

    def _no_reresolve(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the payload builder must not re-resolve the agent")

    monkeypatch.setattr("gobby.workflows.agent_resolver.resolve_agent", _no_reresolve)
    request = _request(project_id=project_id, agent_body=body, agent_lookup_name="developer")

    codex = _build(temp_db, codex_run, request)

    assert codex.prompt == f"{_PERSONA}\n\nimplement the thing"
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables["_agent_context_injected"] is True

    claude_session = _insert_session(temp_db, project_id, machine_id)
    claude_run = _run(machine_id=machine_id, session_id=claude_session, provider="claude")
    claude = _build(temp_db, claude_run, request)

    assert claude.prompt == "implement the thing"
    claude_variables = SessionVariableManager(temp_db).get_variables(claude_session)
    assert "_agent_context_injected" not in claude_variables


def test_monitor_from_tmux_config(temp_db: HubDatabase) -> None:
    tmux = TmuxConfig(
        auto_enter_agent_terminals=False,
        auto_enter_agent_interval_seconds=45,
        auto_enter_approval_prompts=False,
        init_timeout_seconds=90,
    )

    payload = _build(temp_db, _run(), _request(), tmux=tmux)

    assert payload.monitor == SpawnMonitor(
        auto_enter_agent_terminals=False,
        auto_enter_agent_interval_seconds=45,
        auto_enter_approval_prompts=False,
        init_timeout_seconds=90,
    )


def test_task_id_from_run(temp_db: HubDatabase) -> None:
    task_id = str(uuid.uuid4())

    assert _build(temp_db, _run(task_id=task_id), _request()).task_id == task_id
    assert _build(temp_db, _run(task_id=None), _request()).task_id is None


def test_launch_fields_and_workspace(temp_db: HubDatabase) -> None:
    developer = make_agent_definition(
        name="developer",
        prompts={"agent": _PERSONA},
        prewarm_pre_commit_store=False,
        checkout_mode="inherit",
        base_branch="inherit",
    )
    clone_body = make_agent_definition(
        name="developer",
        prompts={"agent": _PERSONA},
        checkout_mode="clone",
        base_branch="0.5.0",
    )
    worktree_body = make_agent_definition(
        name="developer",
        prompts={"agent": _PERSONA},
        checkout_mode="worktree",
        base_branch="inherit",
    )
    run = _run()

    payload = _build(
        temp_db,
        run,
        _request(agent_lookup_name="developer", agent_body=developer, droid_mode="interactive"),
    )

    assert payload.run_id == run.id
    assert payload.session_id == run.child_session_id
    assert payload.parent_session_id == run.parent_session_id
    assert payload.project_id == _PROJECT_ID
    assert payload.terminal_id == _TERMINAL_ID
    assert (payload.agent_depth, payload.max_agent_depth) == (1, 5)
    assert payload.title == "lane 5"
    assert payload.source_root == _SOURCE_ROOT
    assert payload.provider == "claude"
    assert payload.model == "gpt-test"
    assert payload.prompt == "implement the thing"
    assert payload.provider_args == []
    assert payload.reasoning_effort == "medium"
    assert payload.workflow_name == "developer"
    assert payload.agent_name == "developer"
    assert payload.droid_mode == "interactive"
    assert payload.prewarm_pre_commit_store is False
    assert payload.code_index_preflight == "managed"
    assert payload.resume_session_id is None
    assert payload.execution_mode is None
    assert SpawnPayload.model_validate(payload.model_dump()) == payload

    assert _build(temp_db, run, _request(agent_body=developer)).agent_name == "developer"

    assert _build(temp_db, run, _request(timeout=600.0)).timeout_seconds == 600
    assert _build(temp_db, run, _request(agent_body=clone_body)).timeout_seconds == 0
    timed = make_agent_definition(name="developer", prompts={"agent": _PERSONA}, timeout=900)
    assert _build(temp_db, run, _request(agent_body=timed)).timeout_seconds == 900

    run_branch = f"agent/{run.id}"
    unmapped = {"mode": "none", "path": None, "branch_name": None, "base_branch": None}
    workspace_cases: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (
            _request(checkout_mode="worktree", branch_name="feat/x", agent_body=clone_body),
            {"mode": "worktree", "path": None, "branch_name": "feat/x", "base_branch": "0.5.0"},
        ),
        (
            _request(agent_body=clone_body),
            {"mode": "clone", "path": None, "branch_name": run_branch, "base_branch": "0.5.0"},
        ),
        (_request(agent_body=developer), unmapped),
        (
            _request(checkout_mode="worktree", base_branch="main", agent_body=clone_body),
            {"mode": "worktree", "path": None, "branch_name": run_branch, "base_branch": "main"},
        ),
        (
            _request(checkout_mode="worktree", base_branch="inherit", agent_body=clone_body),
            {"mode": "worktree", "path": None, "branch_name": run_branch, "base_branch": None},
        ),
        (
            _request(checkout_mode="none", branch_name="ignored", agent_body=clone_body),
            unmapped,
        ),
        (_request(), unmapped),
        (
            _request(checkout_mode="clone", agent_body=worktree_body),
            {"mode": "clone", "path": None, "branch_name": run_branch, "base_branch": None},
        ),
    ]
    for request, expected in workspace_cases:
        assert _build(temp_db, run, request).workspace.model_dump() == expected


def test_resume_uses_existing_workspace(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    local = require_machine_id()
    insert_isolated_machine(temp_db, local)
    remote = insert_isolated_machine(temp_db)
    mine = _insert_session(temp_db, project_id, local)
    other = _insert_session(temp_db, project_id, local)
    theirs = _insert_session(temp_db, project_id, remote)
    run = _run(machine_id=local, session_id=mine)
    remote_run = _run(machine_id=local, session_id=theirs)
    resume = {"resume_existing_session": True, "provider_native_session_id": _NATIVE_ID}
    clone_body = make_agent_definition(
        name="developer",
        prompts={"agent": _PERSONA},
        checkout_mode="clone",
        base_branch="0.5.0",
    )

    def existing(path: str, branch: str) -> dict[str, Any]:
        return {"mode": "existing", "path": path, "branch_name": branch, "base_branch": None}

    unmapped = {"mode": "none", "path": None, "branch_name": None, "base_branch": None}
    scenarios: list[tuple[AgentRun, list[dict[str, Any]], dict[str, Any], dict[str, Any]]] = [
        # A clone recorded later than an older active worktree wins.
        (
            run,
            [
                _row("worktree", session=mine, machine=local, path="/wt/a", branch="a", updated=1),
                _row("clone", session=mine, machine=local, path="/cl/a", branch="a-c", updated=2),
            ],
            {},
            existing("/cl/a", "a-c"),
        ),
        # A worktree refreshed after a clone was recorded wins despite its older created_at.
        (
            run,
            [
                _row(
                    "clone",
                    session=mine,
                    machine=local,
                    path="/cl/b",
                    branch="b-c",
                    updated=2,
                    created=2,
                ),
                _row("worktree", session=mine, machine=local, path="/wt/b", branch="b", updated=3),
            ],
            {},
            existing("/wt/b", "b"),
        ),
        # Equal updated_at: the greater created_at wins, whichever kind it is.
        (
            run,
            [
                _row(
                    "worktree",
                    session=mine,
                    machine=local,
                    path="/wt/c1",
                    branch="c1-w",
                    updated=5,
                    created=1,
                ),
                _row(
                    "clone",
                    session=mine,
                    machine=local,
                    path="/cl/c1",
                    branch="c1-c",
                    updated=5,
                    created=2,
                ),
            ],
            {},
            existing("/cl/c1", "c1-c"),
        ),
        (
            run,
            [
                _row(
                    "worktree",
                    session=mine,
                    machine=local,
                    path="/wt/c2",
                    branch="c2-w",
                    updated=5,
                    created=2,
                ),
                _row(
                    "clone",
                    session=mine,
                    machine=local,
                    path="/cl/c2",
                    branch="c2-c",
                    updated=5,
                    created=1,
                ),
            ],
            {},
            existing("/wt/c2", "c2-w"),
        ),
        # A row on another machine is ignored: the run's machine is local.
        (
            remote_run,
            [
                _row(
                    "worktree",
                    session=theirs,
                    machine=remote,
                    path="/wt/theirs",
                    branch="t",
                    updated=9,
                ),
            ],
            {"agent_body": clone_body},
            {
                "mode": "clone",
                "path": None,
                "branch_name": f"agent/{remote_run.id}",
                "base_branch": "0.5.0",
            },
        ),
        # A row for another session is ignored.
        (
            run,
            [
                _row(
                    "worktree",
                    session=other,
                    machine=local,
                    path="/wt/other",
                    branch="o",
                    updated=9,
                ),
            ],
            {},
            unmapped,
        ),
        # An inactive row is ignored in favour of an active one.
        (
            run,
            [
                _row(
                    "worktree",
                    session=mine,
                    machine=local,
                    path="/wt/gone",
                    branch="gone",
                    updated=9,
                    status="removed",
                ),
                _row(
                    "clone", session=mine, machine=local, path="/cl/live", branch="live", updated=1
                ),
            ],
            {},
            existing("/cl/live", "live"),
        ),
        # A requested isolation is never replaced by the resumed workspace.
        (
            run,
            [
                _row(
                    "worktree",
                    session=mine,
                    machine=local,
                    path="/wt/mine",
                    branch="mine",
                    updated=9,
                ),
            ],
            {"checkout_mode": "none"},
            unmapped,
        ),
        (
            run,
            [
                _row(
                    "worktree",
                    session=mine,
                    machine=local,
                    path="/wt/mine",
                    branch="mine",
                    updated=9,
                ),
            ],
            {"checkout_mode": "worktree", "branch_name": "feat/y"},
            {"mode": "worktree", "path": None, "branch_name": "feat/y", "base_branch": None},
        ),
        # With no row, the workspace maps as for a new spawn.
        (
            run,
            [],
            {"agent_body": clone_body},
            {
                "mode": "clone",
                "path": None,
                "branch_name": f"agent/{run.id}",
                "base_branch": "0.5.0",
            },
        ),
        (run, [], {}, unmapped),
    ]

    for scenario_run, rows, overrides, expected in scenarios:
        _seed_workspaces(temp_db, project_id, rows)
        payload = _build(
            temp_db,
            scenario_run,
            _request(project_id=project_id, **overrides),
            resume_metadata=resume,
        )
        assert payload.workspace.model_dump() == expected
