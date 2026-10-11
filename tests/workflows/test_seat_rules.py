"""Tests for the bundled `roles` rule group (shared seat guidance and seat policy)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.mcp_proxy.services.result_handling import (
    apply_before_tool_enforcement,
    build_before_tool_event,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

SESSION_ID = "22222222-2222-4222-8222-222222222222"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
ROLES_DIR = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules/roles"
GUIDANCE_HEADING = "## Seat Guidance"

ENHANCER_CALL = {"agent": "plan-enhancer-taskless-old", "checkout_mode": "none"}
DEVELOPER: dict[str, Any] = {"_agent_type": "developer"}
ASSISTANT: dict[str, Any] = {"_agent_type": "assistant"}
ARCHIVIST: dict[str, Any] = {"_agent_type": "archivist"}
METRICS_MONITOR: dict[str, Any] = {"_agent_type": "metrics-monitor"}


def _metrics_queries() -> list[str]:
    definition = yaml.safe_load(
        (ROLES_DIR.parent.parent / "agents/metrics-monitor.yaml").read_text()
    )
    return [
        line.strip()
        for line in definition["prompts"]["agent"].splitlines()
        if line.strip().startswith("/usr/bin/curl ")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, key", [("Bash", "command"), ("exec_command", "cmd")])
async def test_metrics_monitor_allows_only_exact_get_queries(
    engine: RuleEngine, tool: str, key: str
) -> None:
    queries = _metrics_queries()
    assert len(queries) == 2
    for query in queries:
        event: dict[str, Any] = {"tool_name": tool, "tool_input": {key: query}}
        assert (await _decide(engine, event, METRICS_MONITOR)).decision == "allow"
        for command in (
            query + " > /tmp/metrics.json",
            query + "; ps e",
            query.replace("--get", "--request POST"),
            query.replace("127.0.0.1:9090", "example.com"),
            query.replace("--disable ", ""),
            "ps e",
            "env",
            "uv run gobby restart",
        ):
            blocked = await _decide(
                engine, {"tool_name": tool, "tool_input": {key: command}}, METRICS_MONITOR
            )
            assert blocked.decision == "block", command
            assert "Metrics Monitor permits only" in (blocked.reason or "")
        event["tool_input"]["shell"] = "/tmp/other-shell"
        assert (await _decide(engine, event, METRICS_MONITOR)).decision == "block"
    # Other seats retain their own shell policy.
    assert (await _decide(engine, _shell("env"), DEVELOPER)).decision == "allow"


PLAN_WRITER: dict[str, Any] = {"_agent_type": "plan-writer"}
PLAN_ENHANCER: dict[str, Any] = {"_agent_type": "plan-enhancer"}
ORCHESTRATOR: dict[str, Any] = {"_agent_type": "orchestrator"}


def _load_roles(db: HubDatabase) -> None:
    manager = RuleDefinitionManager(db)
    for rule_file in sorted(ROLES_DIR.glob("*.yaml")):
        document = yaml.safe_load(rule_file.read_text())
        for name, rule_data in document["rules"].items():
            body_data, metadata = split_rule_definition_data(rule_data)
            manager.create(
                name=name,
                definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
                priority=metadata["priority"],
                enabled=metadata["enabled"],
                tags=document["tags"],
            )


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    _load_roles(temp_db)
    return RuleEngine(temp_db)


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


async def _turn_context(engine: RuleEngine, variables: dict[str, Any]) -> str:
    response = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "hello"}),
        session_id=SESSION_ID,
        variables=variables,
    )
    return response.context or ""


def _proxy_call(server: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {"server_name": server, "tool_name": tool, "arguments": arguments or {}},
    }


def _shell(command: str) -> dict[str, Any]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


async def _decide(
    engine: RuleEngine, data: dict[str, Any], variables: dict[str, Any]
) -> HookResponse:
    return await engine.evaluate(
        _event(HookEventType.BEFORE_TOOL, data), session_id=SESSION_ID, variables=dict(variables)
    )


@pytest.mark.asyncio
async def test_seat_common_injected_once_per_epoch(engine: RuleEngine) -> None:
    variables = dict(PLAN_WRITER)

    first = await _turn_context(engine, variables)
    second = await _turn_context(engine, variables)

    assert GUIDANCE_HEADING in first
    assert "only when the operator asks" in first
    assert "Only the operator authorizes a push, one exact SHA at a time." in first
    assert 'Say "Systems nominal" when healthy' in first
    assert "may recover a stalled session with `gobby-sessions:send_keys`" in first
    assert GUIDANCE_HEADING not in second


@pytest.mark.asyncio
async def test_seat_common_matches_spawned_and_skips_non_seats(engine: RuleEngine) -> None:
    spawned = await _turn_context(engine, {"_agent_type": "developer"})
    orchestrator = await _turn_context(engine, dict(ORCHESTRATOR))
    plain = await _turn_context(engine, {})
    enhancer = await _turn_context(engine, dict(PLAN_ENHANCER))
    off_catalogue = await _turn_context(engine, {"_agent_type": "design-lead"})

    assert GUIDANCE_HEADING in spawned
    assert GUIDANCE_HEADING in orchestrator
    assert GUIDANCE_HEADING in enhancer
    assert GUIDANCE_HEADING not in plain
    assert GUIDANCE_HEADING not in off_catalogue


@pytest.mark.asyncio
async def test_seat_common_rearms_after_compact(engine: RuleEngine) -> None:
    variables = dict(PLAN_WRITER)
    assert GUIDANCE_HEADING in await _turn_context(engine, variables)

    await engine.evaluate(
        _event(HookEventType.SESSION_START, {"source": "compact"}),
        session_id=SESSION_ID,
        variables=variables,
    )

    assert GUIDANCE_HEADING in await _turn_context(engine, variables)


PIPELINE_LAUNCHES = (
    _proxy_call("gobby-workflows", "run_pipeline", {"name": "nightly"}),
    _proxy_call("gobby-workflows", "pipeline:nightly"),
    _shell("cd /tmp && uv run gobby pipelines run nightly"),
    _shell("gobby --config /tmp/gobby.yaml pipelines run nightly"),
    _shell("python -m gobby.cli pipelines run nightly"),
    _shell("uv run --module gobby.cli pipelines run nightly"),
)


@pytest.mark.asyncio
async def test_seats_cannot_spawn(engine: RuleEngine) -> None:
    for seat in (DEVELOPER, ASSISTANT, PLAN_WRITER, PLAN_ENHANCER):
        for tool in ("spawn_agent", "dispatch_batch"):
            blocked = await _decide(
                engine, _proxy_call("gobby-agents", tool, {"agent": "developer"}), seat
            )
            assert blocked.decision == "block", (seat, tool)
            assert "Seats do not spawn agents" in (blocked.reason or "")

    shell_spawn = _shell("gobby agents spawn developer --task '#1'")
    for seat in (DEVELOPER, ASSISTANT, ORCHESTRATOR, PLAN_WRITER, PLAN_ENHANCER):
        blocked = await _decide(engine, shell_spawn, seat)
        assert blocked.decision == "block", seat
        assert "send the request to the Orchestrator" in (blocked.reason or "")
        allowed = await _decide(
            engine, _proxy_call("gobby-workflows", "set_variable", {"name": "x"}), seat
        )
        assert allowed.decision == "allow", seat

    non_seat = {"_agent_type": "default"}
    for call in (_proxy_call("gobby-agents", "spawn_agent"), shell_spawn, *PIPELINE_LAUNCHES):
        assert (await _decide(engine, call, non_seat)).decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_type", ("lane-manager", "developer", "code-reviewer", "archivist", "plan-writer")
)
async def test_other_seats_cannot_launch_pipelines(engine: RuleEngine, agent_type: str) -> None:
    for launch in PIPELINE_LAUNCHES:
        blocked = await _decide(engine, launch, {"_agent_type": agent_type})
        assert blocked.decision == "block", launch
        assert "Only the Orchestrator launches pipelines" in (blocked.reason or "")
        assert "send the request to the Orchestrator" in (blocked.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ("orchestrator", "assistant"))
async def test_orchestrator_and_assistant_launch_pipelines(
    engine: RuleEngine, agent_type: str
) -> None:
    for launch in PIPELINE_LAUNCHES:
        allowed = await _decide(engine, launch, {"_agent_type": agent_type})
        assert allowed.decision == "allow", launch


RECOVERY_SEATS = ("assistant", "orchestrator", "lane-manager")
WORKER_SEATS = ("developer", "code-reviewer", "plan-writer")
RESUME = {"agent": "developer", "resume_session_id": "gobby#15771"}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", RECOVERY_SEATS)
async def test_recovery_seats_resume_an_existing_session(
    engine: RuleEngine, agent_type: str
) -> None:
    seat = {"_agent_type": agent_type}

    resumed = await _decide(engine, _proxy_call("gobby-agents", "spawn_agent", RESUME), seat)
    new_spawn = await _decide(
        engine, _proxy_call("gobby-agents", "spawn_agent", {"agent": "developer"}), seat
    )
    batch = await _decide(engine, _proxy_call("gobby-agents", "dispatch_batch", RESUME), seat)

    assert resumed.decision == "allow"
    for refused in (new_spawn, batch):
        assert refused.decision == "block"
        assert "resume_session_id" in (refused.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", WORKER_SEATS)
async def test_other_seats_cannot_resume_or_spawn(engine: RuleEngine, agent_type: str) -> None:
    seat = {"_agent_type": agent_type}

    for arguments in (RESUME, {"agent": "developer"}):
        refused = await _decide(engine, _proxy_call("gobby-agents", "spawn_agent", arguments), seat)
        assert refused.decision == "block", arguments
        assert "Seats do not spawn agents" in (refused.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("seat", (ORCHESTRATOR, ASSISTANT))
async def test_orchestrator_still_cannot_spawn_from_the_shell(
    engine: RuleEngine, seat: dict[str, Any]
) -> None:
    for command in (
        "gobby agents spawn developer --task '#1'",
        "uv run gobby agents spawn developer --resume gobby#15771",
        "gobby --config /tmp/gobby.yaml agents spawn developer",
        "python -m gobby.cli agents spawn developer",
        "uv run -m gobby.cli agents spawn developer",
        "gobby pipelines run nightly && gobby agents spawn developer",
    ):
        refused = await _decide(engine, _shell(command), seat)
        assert refused.decision == "block", command
        assert "Seats do not spawn agents from the shell" in (refused.reason or "")


def _seat_definitions() -> list[str]:
    agents_dir = ROLES_DIR.parents[1] / "agents"
    definitions = (yaml.safe_load(path.read_text()) for path in sorted(agents_dir.glob("*.yaml")))
    return [raw["name"] for raw in definitions if "seat" in raw.get("tags", [])]


@pytest.mark.asyncio
async def test_seats_ask_through_send_message(engine: RuleEngine) -> None:
    ask = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "Land?"}]}}
    seats = _seat_definitions()
    assert {"assistant", "orchestrator", "inbox-manager", "release-manager"} <= set(seats)

    for agent_type in (*seats, "plan-writer", "plan-enhancer", "plan-adversary"):
        if agent_type == "assistant":
            assert (await _decide(engine, ask, ASSISTANT)).decision == "allow"
            continue
        blocked = await _decide(engine, ask, {"_agent_type": agent_type})
        assert blocked.decision == "block", agent_type
        assert "Use `gobby-agents:send_message` to send your question" in (blocked.reason or "")
    assert (await _decide(engine, _shell("ls"), DEVELOPER)).decision == "allow"

    for non_seat in ({"_agent_type": "default"}, {}):
        assert (await _decide(engine, ask, non_seat)).decision == "allow", non_seat


def test_every_seat_scoped_rule_covers_every_seat_definition() -> None:
    seats = _seat_definitions()
    scoped: dict[str, str] = {}
    for rule_file in sorted(ROLES_DIR.glob("*.yaml")):
        for name, rule in yaml.safe_load(rule_file.read_text())["rules"].items():
            when = str(rule.get("when") or "")
            if "variables.get('_agent_type') in [" in when:
                scoped[name] = when

    assert {
        "inject-seat-common",
        "seat-no-spawn",
        "seat-no-pipeline-launch",
        "seat-no-pipeline-cli",
        "seat-no-launch-cli",
        "seat-no-ask-user-question",
    } <= set(scoped)
    for name, when in scoped.items():
        exemptions = {"assistant"} if name == "seat-no-ask-user-question" else set()
        assert [
            seat for seat in seats if seat not in exemptions and f"'{seat}'" not in when
        ] == [], name


PROJECT_CREATES = (
    {"tool_name": "mcp__gobby__init_project", "tool_input": {"name": "scratch"}},
    _shell("gobby init"),
    _shell("uv run gobby init --help"),
    _shell("uv run --directory /tmp/scratch gobby init"),
    _shell("uv --directory /tmp/scratch run gobby init --help"),
    _shell("uv run --project /tmp/scratch gobby init"),
    _shell("GOBBY_HOME=/tmp/home gobby init --name scratch"),
    _shell("/Users/operator/.local/bin/gobby init"),
    _shell("gobby --config /tmp/gobby.yaml init"),
    _shell("python -m gobby.cli init"),
    _shell("uv run python -m gobby.cli init"),
    _shell("uv run -m gobby.cli init"),
    _shell("uv run -qm gobby.cli init"),
    _shell("python3 -m gobby.cli install --no-interactive"),
    _shell("cd /tmp/scratch; gobby init"),
    _shell("true && gobby init"),
    _shell("false || gobby init"),
    _shell("echo y | gobby init"),
    _shell("(cd /tmp/scratch && gobby init)"),
    _shell("bash -c 'gobby init'"),
    _shell("gobby install --no-interactive"),
    _shell("curl -s -X POST http://127.0.0.1:60887/api/projects/init -d '{}'"),
    _shell("python -c \"import httpx; httpx.post('http://127.0.0.1:60887/api/projects/init')\""),
    _shell("grep -c init setup.cfg; curl -X POST http://127.0.0.1:60887/api/projects/init"),
)
NOT_PROJECT_CREATES = (
    _shell("git init /tmp/scratch"),
    _shell("gobby initial"),
    _shell("grep -rn 'gobby init' docs"),
    _shell("echo gobby init"),
    _shell("gobby install"),
    _shell("grep -rn /api/projects/init src"),
    _shell("gcode grep -F /api/projects/init -m 5 src"),
)
PROJECT_CREATE_BLOCKED: tuple[dict[str, Any], ...] = (
    DEVELOPER,
    {"_agent_type": "lane-manager"},
    {"is_spawned_agent": True},
    {"_agent_type": "default", "is_spawned_agent": True},
    {"_agent_type": "task-close-reviewer", "is_spawned_agent": True},
)
PROJECT_CREATE_ALLOWED: tuple[dict[str, Any], ...] = (
    ASSISTANT,
    ORCHESTRATOR,
    {"_agent_type": "assistant", "is_spawned_agent": True},
    {"_agent_type": "orchestrator", "is_spawned_agent": True},
    {"_agent_type": "default"},
    {},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("variables", PROJECT_CREATE_BLOCKED)
async def test_agents_cannot_create_projects(engine: RuleEngine, variables: dict[str, Any]) -> None:
    for call in PROJECT_CREATES:
        blocked = await _decide(engine, call, variables)
        assert blocked.decision == "block", call
        assert "project creation needs the operator's go through the Orchestrator" in (
            blocked.reason or ""
        )
    for call in NOT_PROJECT_CREATES:
        assert (await _decide(engine, call, variables)).decision == "allow", call


@pytest.mark.asyncio
@pytest.mark.parametrize("variables", PROJECT_CREATE_ALLOWED)
async def test_assistant_orchestrator_and_root_sessions_create_projects(
    engine: RuleEngine, variables: dict[str, Any]
) -> None:
    for call in PROJECT_CREATES:
        assert (await _decide(engine, call, variables)).decision == "allow", call


def _write(project: Path, file_path: str) -> dict[str, Any]:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "x"},
        "cwd": str(project),
        "project_path": str(project),
    }


@pytest.mark.asyncio
async def test_seat_write_scope_is_path_aware(
    engine: RuleEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = (tmp_path / "checkout").resolve()
    (project / "docs").mkdir(parents=True)
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("HOME", str(home))
    digest_dir = f"{home}/Desktop"
    opaque = {**_shell("git apply update.patch"), "cwd": str(project)}

    async def decision(seat: dict[str, Any], data: dict[str, Any]) -> str:
        return (await _decide(engine, data, seat)).decision

    assert await decision(ASSISTANT, _write(project, "docs/guide.md")) == "allow"
    for path in ("src/app.py", "docs-other/guide.md", "docs/../src/app.py"):
        blocked = await _decide(engine, _write(project, path), ASSISTANT)
        assert blocked.decision == "block", path
        assert "The Assistant writes only under" in (blocked.reason or "")

    digest = f"{digest_dir}/gobby-digest-2026-09-27.md"
    assert await decision(ARCHIVIST, _write(project, digest)) == "allow"
    for path in (
        f"{digest_dir}/notes.md",
        f"{digest_dir}/gobby-digest.md",
        f"{digest_dir}/gobby-digest-2026-09-27-v2.md",
        f"{tmp_path.resolve()}/other-home/Desktop/gobby-digest-2026-09-27.md",
        "docs/guide.md",
    ):
        blocked = await _decide(engine, _write(project, path), ARCHIVIST)
        assert blocked.decision == "block", path
        assert "The Archivist writes only the dated desktop digest" in (blocked.reason or "")

    for seat in (ASSISTANT, ARCHIVIST):
        assert await decision(seat, opaque) == "block"
    assert await decision(DEVELOPER, _write(project, "src/app.py")) == "allow"


class _ProxyHarness:
    """Drive the proxy before_tool boundary with the real event builder."""

    def __init__(self, db: HubDatabase, root: Path, runtime: WorkflowEvaluationRuntime) -> None:
        _load_roles(db)
        checkout = install_isolated_checkout_project(
            db, root, name="seat-policy", machine_id=LOCAL_MACHINE_ID
        )
        self.project_id = checkout.project.id
        self.project_path = root.resolve()
        self.tasks = LocalTaskManager(db)
        self.variables = SessionVariableManager(db)
        self.session_manager = SessionManager(db)
        self.workflow_handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db, task_manager=self.tasks),
            enabled=True,
            evaluation_runtime=runtime,
        )
        self._hook_manager = SimpleNamespace(_workflow_handler=self.workflow_handler)

    def _get_effective_session_id(self, session_id: str | None) -> str | None:
        return session_id

    def _resolve_hook_manager(self) -> Any:
        return self._hook_manager

    def _resolve_tool_event_context(
        self, effective_session_id: str
    ) -> tuple[Any, SessionManager, Any, SessionSource, dict[str, Any], str, str]:
        return (
            self._hook_manager,
            self.session_manager,
            self.session_manager.get(effective_session_id),
            SessionSource.CLAUDE,
            {"_platform_session_id": effective_session_id},
            str(self.project_path),
            self.project_id,
        )

    def _build_before_tool_event(
        self,
        *,
        effective_session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> HookEvent:
        return build_before_tool_event(
            self,
            effective_session_id=effective_session_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )

    def _prepare_arguments(
        self, arguments: Any
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return (arguments if isinstance(arguments, dict) else {}, None)

    def task(self, title: str) -> str:
        return self.tasks.create_task(
            project_id=self.project_id, title=title, validation_criteria="Plan is enhanced once."
        ).id

    def session(self, name: str, seat: dict[str, Any], claim: str | None = None) -> str:
        session_id = self.session_manager.register_session(
            external_id=f"external-{name}",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=self.project_id,
            project_path=str(self.project_path),
            agent_depth=0,
        )
        self.variables.merge_variables(session_id, dict(seat))
        if claim is not None:
            self.claim(session_id, claim)
        return session_id

    def claim(self, session_id: str, *task_ids: str) -> None:
        claimed = {task_id: f"#{self.tasks.get_task(task_id).seq_num}" for task_id in task_ids}
        self.variables.merge_variables(session_id, {"task_claimed": True, "claimed_tasks": claimed})

    async def call(
        self, session_id: str, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        _, _, _, error, _ = await apply_before_tool_enforcement(
            self, server, tool, arguments=dict(arguments), session_id=session_id
        )
        return error

    def _hook_event(self, session_id: str, event_type: HookEventType, data: dict[str, Any]) -> Any:
        return HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data=data,
            metadata={"_platform_session_id": session_id},
            cwd=str(self.project_path),
            project_id=self.project_id,
        )

    async def provider_hook(self, session_id: str) -> str:
        """The provider CLI's own before_tool evaluation of the same call."""
        event = self._hook_event(
            session_id,
            HookEventType.BEFORE_TOOL,
            _proxy_call("gobby-agents", "spawn_agent", dict(ENHANCER_CALL)),
        )
        return (await self.workflow_handler.evaluate_async(event)).decision


@pytest.fixture
def workflow_runtime() -> Iterator[WorkflowEvaluationRuntime]:
    runtime = WorkflowEvaluationRuntime()
    try:
        yield runtime
    finally:
        runtime.shutdown()


@pytest.fixture
def harness(
    temp_db: HubDatabase, tmp_path: Path, workflow_runtime: WorkflowEvaluationRuntime
) -> Iterator[_ProxyHarness]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield _ProxyHarness(temp_db, tmp_path / "checkout", workflow_runtime)


@pytest.mark.asyncio
async def test_plan_writer_cannot_spawn_plan_enhancer_taskless_old(harness: _ProxyHarness) -> None:
    """The planning runbook launches the live Enhancer; the Writer spawns no pass."""
    writer = harness.session("writer", PLAN_WRITER, claim=harness.task("plan"))

    assert await harness.provider_hook(writer) == "block"
    for tool in ("spawn_agent", "dispatch_batch"):
        refused = await harness.call(writer, "gobby-agents", tool, dict(ENHANCER_CALL))
        assert refused is not None and "[seat-no-spawn]" in refused["error"], tool


@pytest.mark.asyncio
async def test_persona_name_alone_matches_no_seat_rule(harness: _ProxyHarness) -> None:
    """A persona overlay switches prompt and skills, never seat rules."""
    plan = harness.task("plan")
    persona = harness.session("persona", {"_persona_name": "plan-writer"}, claim=plan)

    async def hook(event_type: HookEventType, data: dict[str, Any]) -> HookResponse:
        event = harness._hook_event(persona, event_type, data)
        return await harness.workflow_handler.evaluate_async(event)

    turn = await hook(HookEventType.BEFORE_AGENT, {"prompt": "hello"})
    assert GUIDANCE_HEADING not in (turn.context or "")
    shell = await hook(HookEventType.BEFORE_TOOL, _shell("gobby agents spawn developer"))
    assert shell.decision == "allow"
    for server, tool, arguments in (
        ("gobby-agents", "spawn_agent", {"agent": "developer"}),
        ("gobby-agents", "dispatch_batch", dict(ENHANCER_CALL)),
        ("gobby-workflows", "run_pipeline", {"name": "nightly"}),
        ("gobby-agents", "spawn_agent", dict(ENHANCER_CALL)),
    ):
        assert await harness.call(persona, server, tool, arguments) is None, tool
