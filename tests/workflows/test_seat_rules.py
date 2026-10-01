"""Tests for the bundled `roles` rule group (shared seat guidance and seat policy)."""

from __future__ import annotations

import asyncio
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

RECEIPT = "enhancer-pass-spent"
ENHANCER_CALL = {"agent": "plan-enhancer-taskless", "isolation": "none"}
DEVELOPER: dict[str, Any] = {"_agent_type": "developer"}
ASSISTANT: dict[str, Any] = {"_agent_type": "default", "_persona_name": "assistant"}
ARCHIVIST: dict[str, Any] = {"_agent_type": "archivist"}
PLAN_WRITER: dict[str, Any] = {"_agent_type": "default", "_persona_name": "plan-writer"}
ORCHESTRATOR: dict[str, Any] = {"_agent_type": "default", "_persona_name": "orchestrator"}


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
    variables: dict[str, Any] = {"_persona_name": "plan-writer"}

    first = await _turn_context(engine, variables)
    second = await _turn_context(engine, variables)

    assert GUIDANCE_HEADING in first
    assert "only when Josh asks" in first
    assert "Game Goblins jobs are never rerun" in first
    assert "Give no load numbers unless load is breaching" in first
    assert GUIDANCE_HEADING not in second


@pytest.mark.asyncio
async def test_seat_common_matches_spawned_and_skips_non_seats(engine: RuleEngine) -> None:
    spawned = await _turn_context(engine, {"_agent_type": "developer"})
    orchestrator = await _turn_context(engine, {"_persona_name": "orchestrator"})
    plain = await _turn_context(engine, {})
    off_catalogue = await _turn_context(engine, {"_persona_name": "design-lead"})

    assert GUIDANCE_HEADING in spawned
    assert GUIDANCE_HEADING in orchestrator
    assert GUIDANCE_HEADING not in plain
    assert GUIDANCE_HEADING not in off_catalogue


@pytest.mark.asyncio
async def test_seat_common_rearms_after_compact(engine: RuleEngine) -> None:
    variables: dict[str, Any] = {"_persona_name": "plan-writer"}
    assert GUIDANCE_HEADING in await _turn_context(engine, variables)

    await engine.evaluate(
        _event(HookEventType.SESSION_START, {"source": "compact"}),
        session_id=SESSION_ID,
        variables=variables,
    )

    assert GUIDANCE_HEADING in await _turn_context(engine, variables)


@pytest.mark.asyncio
async def test_seats_cannot_spawn(engine: RuleEngine) -> None:
    for seat in (DEVELOPER, ASSISTANT):
        for tool in ("spawn_agent", "dispatch_batch"):
            blocked = await _decide(
                engine, _proxy_call("gobby-agents", tool, {"agent": "developer"}), seat
            )
            assert blocked.decision == "block", (seat, tool)
            assert "Seats do not spawn agents" in (blocked.reason or "")

    launches = [
        _proxy_call("gobby-workflows", "run_pipeline", {"name": "nightly"}),
        _proxy_call("gobby-workflows", "pipeline:nightly"),
        _shell("gobby agents spawn developer --task '#1'"),
        _shell("cd /tmp && uv run gobby pipelines run nightly"),
    ]
    for seat in (DEVELOPER, ASSISTANT, PLAN_WRITER):
        for launch in launches:
            blocked = await _decide(engine, launch, seat)
            assert blocked.decision == "block", (seat, launch)
            assert "send the request to the Orchestrator" in (blocked.reason or "")
        allowed = await _decide(
            engine, _proxy_call("gobby-workflows", "set_variable", {"name": "x"}), seat
        )
        assert allowed.decision == "allow", seat

    non_seat = {"_agent_type": "default"}
    for call in (_proxy_call("gobby-agents", "spawn_agent"), launches[0], launches[2]):
        assert (await _decide(engine, call, non_seat)).decision == "allow"


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
    assert await decision(ASSISTANT, _write(project, ".gobby/roles/_common.md")) == "allow"
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


class _ReceiptDispatcher:
    """Inline mcp_call dispatcher that writes task labels through storage."""

    def __init__(self, tasks: LocalTaskManager) -> None:
        self.tasks = tasks
        self.receipt_calls = 0
        self.fail = False

    async def __call__(
        self, server: str, tool: str, arguments: dict[str, Any], event: Any
    ) -> dict[str, Any]:
        assert (server, tool) == ("gobby-tasks", "add_label")
        self.receipt_calls += 1
        if self.fail:
            return {"success": False, "result": {"error": "receipt store unavailable"}}
        task = self.tasks.add_label(arguments["task_id"], arguments["label"])
        return {"success": True, "result": {"task_id": task.id}}


class _ProxyHarness:
    """Drive the proxy before_tool boundary with the real event builder."""

    def __init__(self, db: HubDatabase, root: Path, runtime: WorkflowEvaluationRuntime) -> None:
        _load_roles(db)
        checkout = install_isolated_checkout_project(
            db, root, name="seat-policy", machine_id=LOCAL_MACHINE_ID
        )
        self.project_id = checkout.project.id
        self.project_path = root.resolve()
        self.db = db
        self.tasks = LocalTaskManager(db)
        self.dispatcher = _ReceiptDispatcher(self.tasks)
        self.variables = SessionVariableManager(db)
        self.session_manager = SessionManager(db)
        self.workflow_handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db, mcp_dispatcher=self.dispatcher, task_manager=self.tasks),
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

    def receipted(self, task_id: str) -> bool:
        return RECEIPT in (self.tasks.get_task(task_id).labels or [])

    async def call(
        self, session_id: str, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        _, _, _, error, _ = await apply_before_tool_enforcement(
            self, server, tool, arguments=dict(arguments), session_id=session_id
        )
        return error

    async def spawn(self, session_id: str, **arguments: Any) -> dict[str, Any] | None:
        return await self.call(session_id, "gobby-agents", "spawn_agent", arguments)

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

    async def session_start(self, session_id: str, source: str) -> None:
        event = self._hook_event(session_id, HookEventType.SESSION_START, {"source": source})
        await self.workflow_handler.evaluate_async(event)


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
async def test_plan_writer_enhancer_pass_is_per_task(harness: _ProxyHarness) -> None:
    first, second, third = (harness.task(f"plan {n}") for n in (1, 2, 3))
    writer = harness.session("writer", PLAN_WRITER, claim=first)
    unclaimed = harness.session("unclaimed", PLAN_WRITER)

    # A different agent, another isolation, or no claimed task is refused.
    for arguments in (
        {"agent": "developer", "isolation": "none"},
        {"agent": "plan-enhancer-taskless", "isolation": "worktree"},
        {"agent": "plan-enhancer-taskless"},
    ):
        refused = await harness.spawn(writer, **arguments)
        assert refused is not None and "plan-writer-enhancer-only" in refused["error"]
    assert await harness.spawn(unclaimed, **ENHANCER_CALL) is not None
    assert harness.dispatcher.receipt_calls == 0

    # One call evaluated by the provider hook, then the proxy: one receipt, one dispatch.
    assert await harness.provider_hook(writer) == "allow"
    assert not harness.receipted(first)
    assert await harness.spawn(writer, **ENHANCER_CALL) is None
    assert harness.receipted(first)
    assert harness.dispatcher.receipt_calls == 1

    # A reclaim answered already_claimed or a create_task(claim=true) keeps the same
    # claim; a compact keeps the session; a /clear successor inherits claimed_tasks;
    # a fresh session claims the released task. Each is refused by the task label.
    assert await harness.provider_hook(writer) == "block"
    assert await harness.spawn(writer, **ENHANCER_CALL) is not None
    await harness.session_start(writer, "compact")
    assert await harness.spawn(writer, **ENHANCER_CALL) is not None
    successor = harness.session("clear-successor", PLAN_WRITER, claim=first)
    assert await harness.spawn(successor, **ENHANCER_CALL) is not None
    fresh = harness.session("fresh", PLAN_WRITER, claim=first)
    assert await harness.spawn(fresh, **ENHANCER_CALL) is not None
    assert harness.dispatcher.receipt_calls == 1

    # A different planning task admits exactly one pass, even for concurrent calls.
    harness.claim(writer, second)
    results = await asyncio.gather(
        harness.spawn(writer, **ENHANCER_CALL), harness.spawn(writer, **ENHANCER_CALL)
    )
    assert sum(result is None for result in results) == 1
    assert harness.receipted(second)
    assert harness.dispatcher.receipt_calls == 2

    # A failed receipt write refuses the spawn; a spawn that fails after its
    # receipt keeps the pass spent.
    harness.claim(writer, third)
    harness.dispatcher.fail = True
    failed = await harness.spawn(writer, **ENHANCER_CALL)
    assert failed is not None and "add_label" in failed["error"]
    assert not harness.receipted(third)
    harness.dispatcher.fail = False
    assert await harness.spawn(writer, **ENHANCER_CALL) is None
    assert await harness.spawn(writer, **ENHANCER_CALL) is not None
    assert harness.receipted(third)

    # A seat holding several claims is refused: one receipt cannot spend them all.
    fourth = harness.task("plan 4")
    multi = harness.session("multi", PLAN_WRITER)
    receipts_before = harness.dispatcher.receipt_calls
    for claims in ((first, fourth), (fourth, first)):
        harness.claim(multi, *claims)
        refused = await harness.spawn(multi, **ENHANCER_CALL)
        assert refused is not None and "plan-writer-enhancer-only" in refused["error"]
    assert not harness.receipted(fourth)
    assert harness.dispatcher.receipt_calls == receipts_before

    # Only the Orchestrator removes a receipt.
    remove: dict[str, Any] = {"task_id": first, "label": RECEIPT}
    relabel: dict[str, Any] = {"task_id": first, "labels": ["plan"]}
    for name, seat in (("writer-2", PLAN_WRITER), ("developer", DEVELOPER)):
        other = harness.session(name, seat)
        for tool, arguments in (("remove_label", remove), ("update_task", relabel)):
            refused = await harness.call(other, "gobby-tasks", tool, arguments)
            assert refused is not None and "only the Orchestrator" in refused["error"], tool
    keep = {"task_id": first, "labels": ["plan", RECEIPT]}
    assert await harness.call(writer, "gobby-tasks", "update_task", keep) is None
    orchestrator = harness.session("orchestrator", ORCHESTRATOR)
    assert await harness.call(orchestrator, "gobby-tasks", "remove_label", remove) is None
    assert await harness.call(orchestrator, "gobby-tasks", "update_task", relabel) is None


@pytest.mark.asyncio
async def test_receipt_retention_resolves_supported_task_refs(
    harness: _ProxyHarness, tmp_path: Path
) -> None:
    """Receipt retention protects the task update_task targets for every ref form."""
    plan = harness.task("plan")
    receipted = harness.tasks.create_task(
        project_id=harness.project_id,
        title="planning task",
        validation_criteria="Plan is enhanced once.",
        parent_task_id=plan,
    )
    harness.tasks.add_label(receipted.id, RECEIPT)
    path = harness.tasks.update_path_cache(receipted.id)
    assert path is not None and "." in path
    # Another project reuses both sequence numbers, so an unscoped #N lookup is ambiguous.
    other = install_isolated_checkout_project(
        harness.db, tmp_path / "other", name="other-project", machine_id=LOCAL_MACHINE_ID
    )
    for title in ("other plan", "other task"):
        harness.tasks.create_task(
            project_id=other.project.id, title=title, validation_criteria="Unrelated."
        )
    refs = [receipted.id, f"#{receipted.seq_num}", str(receipted.seq_num), path]

    writer = harness.session("writer", PLAN_WRITER, claim=receipted.id)
    developer = harness.session("developer", DEVELOPER)
    for session_id in (writer, developer):
        for ref in [*refs, "#999", "9.9.9"]:
            relabel = {"task_id": ref, "labels": ["plan"]}
            refused = await harness.call(session_id, "gobby-tasks", "update_task", relabel)
            assert refused is not None and "only the Orchestrator" in refused["error"], ref
    assert harness.receipted(receipted.id)
    assert await harness.spawn(writer, **ENHANCER_CALL) is not None

    for ref in refs:
        keep = {"task_id": ref, "labels": ["plan", RECEIPT]}
        assert await harness.call(writer, "gobby-tasks", "update_task", keep) is None, ref
    unreceipted = {"task_id": f"#{harness.tasks.get_task(plan).seq_num}", "labels": ["plan"]}
    assert await harness.call(developer, "gobby-tasks", "update_task", unreceipted) is None
    orchestrator = harness.session("orchestrator", ORCHESTRATOR)
    for ref in refs:
        relabel = {"task_id": ref, "labels": ["plan"]}
        assert await harness.call(orchestrator, "gobby-tasks", "update_task", relabel) is None
