from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from gobby.ask.errors import AskPermissionDenied
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import (
    get_current_agent_run_id,
    reset_current_agent_run_id,
    session_context_for_test,
    set_current_agent_run_id,
)
from tests.ask.service_support import build_ask_service

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"


class _Result:
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return deepcopy(
            {
                "run_id": "ask-run-1",
                "status": "running",
                "current_stage": "prepare",
                "answer_outcome": None,
                "typed_error": None,
                "deadline_at": "2026-09-09T12:10:00Z",
                "profile_identities": {},
                "tool_identities": [],
                "artifact_manifest": None,
            }
        )


class _AskService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _call(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, values))
        if values.get("project_id") == "foreign-project":
            raise PermissionError("Ask run belongs to another project")
        return {"ok": True, "operation": name}

    async def start(self, request: Any, **kwargs: Any) -> _Result:
        self.calls.append(("start", {"request": request, **kwargs}))
        return _Result()

    def get(self, run_id: str, **kwargs: Any) -> _Result:
        self.calls.append(("get", {"run_id": run_id, **kwargs}))
        return _Result()

    async def wait(self, run_id: str, **kwargs: Any) -> _Result:
        self.calls.append(("wait", {"run_id": run_id, **kwargs}))
        return _Result()

    async def resume(self, run_id: str, **kwargs: Any) -> _Result:
        self.calls.append(("resume", {"run_id": run_id, **kwargs}))
        return _Result()

    async def cancel(self, run_id: str, **kwargs: Any) -> _Result:
        self.calls.append(("cancel", {"run_id": run_id, **kwargs}))
        return _Result()

    def publication_root(self, run_id: str, **kwargs: Any) -> Path:
        self.calls.append(("publication_root", {"run_id": run_id, **kwargs}))
        return Path("/verified/ask-run-1/publication")

    async def prepare(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("prepare", kwargs)

    async def seed(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("seed", kwargs)

    async def spawn(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("spawn", kwargs)

    async def validate(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("validate", kwargs)

    async def admit_repair(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("admit_repair", kwargs)

    async def publish(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("publish", kwargs)

    async def query_evidence(self, **kwargs: Any) -> dict[str, Any]:
        return self._call(
            "query_evidence",
            {**kwargs, "agent_run_id": get_current_agent_run_id()},
        )

    async def read_evidence(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("read_evidence", kwargs)

    async def submit_answer(self, **kwargs: Any) -> dict[str, Any]:
        return self._call(
            "submit_answer",
            {**kwargs, "agent_run_id": get_current_agent_run_id()},
        )

    async def submit_review(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("submit_review", kwargs)


@contextmanager
def _project_context(project_id: str) -> Iterator[None]:
    token = set_project_context({"id": project_id})
    try:
        yield
    finally:
        reset_project_context(token)


@contextmanager
def _agent_run_context(agent_run_id: str) -> Iterator[None]:
    token = set_current_agent_run_id(agent_run_id)
    try:
        yield
    finally:
        reset_current_agent_run_id(token)


@pytest.mark.asyncio
async def test_ask_authorization_and_discovery(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    from gobby.mcp_proxy.tools.ask import create_ask_registry

    project_id = str(sample_project["id"])
    ask = build_ask_service(temp_db, project_id=project_id, state_root=tmp_path / "state")
    resolved_roots: list[str] = []

    def resolve_root(resolved_project_id: str, project_path: str | None) -> Path:
        resolved_roots.append(resolved_project_id)
        return tmp_path

    registry = create_ask_registry(
        lambda _project_id: ask.service,
        project_root_resolver=resolve_root,
    )
    names = {entry["name"] for entry in registry.list_tools()}
    assert names == {
        "start_ask_run",
        "get_ask_run",
        "wait_for_ask_run",
        "resume_ask_run",
        "cancel_ask_run",
        "export_ask_run",
        "prepare",
        "seed",
        "spawn",
        "validate",
        "admit_repair",
        "publish",
        "query_evidence",
        "read_evidence",
        "submit_answer",
        "submit_review",
    }
    start_schema = registry.get_schema("start_ask_run")
    assert start_schema is not None
    assert start_schema["inputSchema"]["required"] == ["question"]
    assert "project_id" not in start_schema["inputSchema"]["properties"]
    query_schema = registry.get_schema("query_evidence")
    assert query_schema is not None
    assert "limit" not in query_schema["inputSchema"]["properties"]

    with _project_context(project_id), session_context_for_test(SESSION_ID):
        started = await registry.call(
            "start_ask_run",
            {"question": "Where is the source of truth?", "retrieval_mode": "hybrid"},
        )
    run_id = started["run_id"]
    # The public operation binds the run to the caller's project and session rather
    # than to anything the caller supplied: start_ask_run takes no project_id.
    record = ask.storage.get(run_id)
    assert record is not None
    assert record.binding.project_id == project_id
    assert record.binding.retrieval_mode.value == "audited_hybrid"
    assert resolved_roots == [project_id]
    assert len(ask.executor.calls) == 1
    execution_id, executed_session_id, _inputs = ask.executor.calls[0]
    assert execution_id == run_id
    assert executed_session_id == SESSION_ID
    completed = await ask.service.wait(run_id, project_id=project_id, timeout=10)
    assert completed.status == "completed"

    # An internal stage call carries no executor authority when an ordinary caller
    # makes it, so the real service refuses it before any stage work begins.
    with _project_context(project_id):
        with pytest.raises(AskPermissionDenied, match="no executor authority is active"):
            await registry.call("prepare", {"run_id": run_id, "project_id": project_id})

    with _project_context(project_id):
        with pytest.raises(PermissionError, match="another project"):
            await registry.call(
                "prepare",
                {"run_id": run_id, "project_id": "foreign-project"},
            )


@pytest.mark.asyncio
async def test_ask_registry_requires_request_project_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy.tools.ask import create_ask_registry

    service = _AskService()
    resolved_projects: list[str] = []

    def resolve_service(project_id: str) -> _AskService:
        resolved_projects.append(project_id)
        return service

    monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)
    registry = create_ask_registry(
        resolve_service,
        project_root_resolver=lambda _project_id, _project_path: tmp_path,
    )

    with pytest.raises(RuntimeError, match="project context is unavailable"):
        await registry.call("get_ask_run", {"run_id": "ask-run-1"})

    assert resolved_projects == []
    assert service.calls == []


@pytest.mark.asyncio
async def test_composed_ask_registry_binds_each_operation_to_the_current_project(
    hub_db: Any,
) -> None:
    from gobby.mcp_proxy.registries import setup_internal_registries

    other_project_id = "22222222-2222-4222-8222-222222222222"
    services = {PROJECT_ID: _AskService(), other_project_id: _AskService()}
    resolved_projects: list[str] = []

    def resolve_service(project_id: str) -> _AskService | None:
        resolved_projects.append(project_id)
        return services.get(project_id)

    manager = setup_internal_registries(
        config_resolver=lambda: None,
        db=hub_db,
        project_id=PROJECT_ID,
        ask_service_resolver=resolve_service,
    )

    registry = manager.get_registry("gobby-ask")
    assert registry is not None
    assert registry.get_schema("start_ask_run") is not None

    for project_id, agent_run_id in (
        (PROJECT_ID, "agent-run-one"),
        (other_project_id, "agent-run-two"),
    ):
        with _project_context(project_id), session_context_for_test(SESSION_ID):
            await registry.call("get_ask_run", {"run_id": "ask-run-1"})
            await registry.call(
                "seed",
                {
                    "run_id": "ask-run-1",
                    "project_id": project_id,
                },
            )
        with _project_context(project_id), _agent_run_context(agent_run_id):
            await registry.call(
                "submit_answer",
                {
                    "run_id": "ask-run-1",
                    "attempt": 2,
                    "draft": {"answer": "bound"},
                    "draft_hash": "draft-hash",
                    "evidence_manifest_hash": "evidence-hash",
                },
            )

        calls = services[project_id].calls
        assert calls[0] == (
            "get",
            {"run_id": "ask-run-1", "project_id": project_id},
        )
        assert calls[1] == (
            "seed",
            {
                "run_id": "ask-run-1",
                "project_id": project_id,
            },
        )
        assert calls[2][0] == "submit_answer"
        assert calls[2][1]["attempt"] == 2
        assert calls[2][1]["agent_run_id"] == agent_run_id

    assert resolved_projects == [PROJECT_ID] * 3 + [other_project_id] * 3


@pytest.mark.asyncio
async def test_ask_registry_refuses_an_unavailable_project_without_foreign_fallback(
    hub_db: Any,
) -> None:
    from gobby.mcp_proxy.registries import setup_internal_registries

    unavailable_project_id = "33333333-3333-4333-8333-333333333333"
    startup_service = _AskService()
    foreign_service = _AskService()
    services = {PROJECT_ID: startup_service, "foreign-project": foreign_service}
    resolved_projects: list[str] = []

    def resolve_service(project_id: str) -> _AskService | None:
        resolved_projects.append(project_id)
        return services.get(project_id)

    manager = setup_internal_registries(
        config_resolver=lambda: None,
        db=hub_db,
        project_id=PROJECT_ID,
        ask_service_resolver=resolve_service,
    )
    registry = manager.get_registry("gobby-ask")
    assert registry is not None

    with _project_context(unavailable_project_id):
        with pytest.raises(RuntimeError, match="unavailable"):
            await registry.call("get_ask_run", {"run_id": "ask-run-1"})

    assert resolved_projects == [unavailable_project_id]
    assert startup_service.calls == []
    assert foreign_service.calls == []


@pytest.mark.asyncio
async def test_native_executor_can_execute_stage_through_real_proxy_registry(
    temp_db: Any,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    import yaml

    from gobby.ask.contracts import AskRequest, ProfileSnapshot
    from gobby.ask.pipeline import parse_ask_pipeline
    from gobby.ask.service import AskService
    from gobby.ask.stages import AskStageStore
    from gobby.ask.storage import AskRunStorage
    from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
    from gobby.mcp_proxy.tools.ask import create_ask_registry
    from gobby.mcp_proxy.tools.internal import InternalRegistryManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.templates import TemplateEngine

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    pipeline_path = (
        Path(__file__).parents[3] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    pipeline = parse_ask_pipeline(yaml.safe_load(pipeline_path.read_text()))

    def resolve_profile(identifier: str, _timeout: float) -> ProfileSnapshot:
        return ProfileSnapshot(
            identifier=identifier,
            definition_id=f"definition-{identifier}",
            definition_updated_at="2026-09-10T12:00:00+00:00",
            effective={"name": identifier, "provider": "codex", "model": "gpt-test"},
        )

    storage = AskRunStorage(
        manager,
        profile_resolver=resolve_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=pipeline.model_dump(mode="json"),
    )
    services: dict[str, AskService] = {}
    registries = InternalRegistryManager()
    registries.add_registry(
        create_ask_registry(
            lambda requested_project_id: (
                services.get("ask") if requested_project_id == project_id else None
            ),
            project_root_resolver=lambda _project_id, _project_path: tmp_path,
        )
    )
    mcp_manager = MagicMock()
    mcp_manager.project_id = project_id
    mcp_manager.session_manager = None
    proxy = ToolProxyService(
        mcp_manager,
        internal_manager=registries,
        validate_arguments=True,
    )
    session_manager = SessionManager(temp_db)
    caller = session_manager.register(
        external_id="native-ask-stage-authority-caller",
        machine_id=None,
        source="codex",
        project_id=project_id,
    )
    executor = PipelineExecutor(
        db=temp_db,
        execution_manager=manager,
        llm_service=object(),
        template_engine=TemplateEngine(),
        tool_proxy_getter=lambda: proxy,
        session_manager=session_manager,
    )
    service = AskService(
        storage=storage,
        stages=AskStageStore(manager),
        snapshot_manager=MagicMock(),
        agents=MagicMock(),
        permissions=MagicMock(),
        pipeline_executor=executor,
        state_root=tmp_path / "state",
    )
    prepare = AsyncMock(return_value={"status": "prepared"})
    seed = AsyncMock(return_value={"status": "seeded"})
    spawn = AsyncMock(side_effect=RuntimeError("stop after authorized stage dispatch"))
    monkeypatch.setattr(service.stage_runtime, "prepare", prepare)
    monkeypatch.setattr(service.stage_runtime, "seed", seed)
    monkeypatch.setattr(service.stage_runtime, "spawn", spawn)
    services["ask"] = service

    started = await service.start(
        AskRequest(
            question="Can the actual executor cross the private stage boundary?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        project_root=tmp_path,
        caller_session_id=caller.id,
    )
    result = await service.wait(started.run_id, project_id=project_id, timeout=10)

    assert result.status == "failed"
    prepare.assert_awaited_once()
    seed.assert_awaited_once()
    assert seed.await_args is not None
    assert seed.await_args.args[0].run_id == started.run_id
    spawn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("server_name", ["gobby-ask", "gobby"])
async def test_ordinary_same_project_caller_cannot_execute_internal_stage_through_proxy(
    temp_db: Any,
    sample_project: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_name: str,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    import yaml

    from gobby.ask.contracts import AskRequest, ProfileSnapshot
    from gobby.ask.pipeline import parse_ask_pipeline
    from gobby.ask.service import AskService
    from gobby.ask.storage import AskRunStorage
    from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
    from gobby.mcp_proxy.tools.ask import create_ask_registry
    from gobby.mcp_proxy.tools.internal import InternalRegistryManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager

    project_id = str(sample_project["id"])
    pipeline_path = (
        Path(__file__).parents[3] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
    )
    pipeline = parse_ask_pipeline(yaml.safe_load(pipeline_path.read_text()))

    def resolve_profile(identifier: str, _timeout: float) -> ProfileSnapshot:
        return ProfileSnapshot(
            identifier=identifier,
            definition_id=f"definition-{identifier}",
            definition_updated_at="2026-09-08T12:00:00+00:00",
            effective={"name": identifier, "provider": "codex", "model": "gpt-test"},
        )

    storage = AskRunStorage(
        LocalPipelineExecutionManager(temp_db, project_id=project_id),
        profile_resolver=resolve_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        pipeline_snapshot=pipeline.model_dump(mode="json"),
    )
    target = storage.start(
        AskRequest(
            question="Which pipeline owns this stage?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    service = AskService(
        storage=storage,
        stages=MagicMock(),
        snapshot_manager=MagicMock(),
        agents=MagicMock(),
        permissions=MagicMock(),
        pipeline_executor=MagicMock(),
        state_root=tmp_path / "state",
    )
    seed = AsyncMock(return_value={"run_id": target.run_id})
    monkeypatch.setattr(service.stage_runtime, "seed", seed)

    with pytest.raises(PermissionError, match="owning Ask pipeline"):
        await service.seed(run_id=target.run_id, project_id=project_id)

    registries = InternalRegistryManager()
    registries.add_registry(
        create_ask_registry(
            lambda requested_project_id: service if requested_project_id == project_id else None,
            project_root_resolver=lambda _project_id, _project_path: tmp_path,
        )
    )
    registry = registries.get_registry("gobby-ask")
    assert registry is not None
    with _project_context(project_id):
        with pytest.raises(PermissionError, match="owning Ask pipeline"):
            await registry.call(
                "seed",
                {"run_id": target.run_id, "project_id": project_id},
            )
    mcp_manager = MagicMock()
    mcp_manager.project_id = project_id
    mcp_manager.session_manager = None
    proxy = ToolProxyService(
        mcp_manager,
        internal_manager=registries,
        validate_arguments=False,
    )

    with _project_context(project_id), session_context_for_test(SESSION_ID):
        result = await proxy.call_tool(
            server_name,
            "seed",
            {"run_id": target.run_id, "project_id": project_id},
            session_id=SESSION_ID,
            enforce_workflow=False,
        )

    assert result.get("success") is False, result
    assert result["error_code"] == "TOOL_BLOCKED"
    assert "owning Ask pipeline" in result["error"]
    seed.assert_not_awaited()
