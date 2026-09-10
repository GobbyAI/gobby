from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from gobby.utils.session_context import session_context_for_test

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
        return self._call("query_evidence", kwargs)

    async def read_evidence(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("read_evidence", kwargs)

    async def submit_answer(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("submit_answer", kwargs)

    async def submit_review(self, **kwargs: Any) -> dict[str, Any]:
        return self._call("submit_review", kwargs)


@pytest.mark.asyncio
async def test_ask_authorization_and_discovery(tmp_path: Path) -> None:
    from gobby.mcp_proxy.tools.ask import create_ask_registry

    service = _AskService()
    registry = create_ask_registry(
        lambda: service,
        project_id=PROJECT_ID,
        project_root_resolver=lambda _project_id: tmp_path,
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

    with session_context_for_test(SESSION_ID):
        started = await registry.call(
            "start_ask_run",
            {"question": "Where is the source of truth?", "retrieval_mode": "hybrid"},
        )
    assert started["run_id"] == "ask-run-1"
    start_call = service.calls[0]
    request = start_call[1]["request"]
    assert request.project_id == PROJECT_ID
    assert request.retrieval_mode.value == "audited_hybrid"
    assert start_call[1]["project_root"] == tmp_path
    assert start_call[1]["caller_session_id"] == SESSION_ID

    prepared = await registry.call(
        "prepare",
        {"run_id": "ask-run-1", "project_id": PROJECT_ID},
    )
    assert prepared == {"ok": True, "operation": "prepare"}

    with pytest.raises(PermissionError, match="another project"):
        await registry.call(
            "prepare",
            {"run_id": "ask-run-1", "project_id": "foreign-project"},
        )


def test_ask_registry_is_composed_for_an_active_project(hub_db: Any) -> None:
    from gobby.mcp_proxy.registries import setup_internal_registries

    service = _AskService()
    manager = setup_internal_registries(
        config_resolver=lambda: None,
        db=hub_db,
        project_id=PROJECT_ID,
        ask_service_resolver=lambda: service,
    )

    registry = manager.get_registry("gobby-ask")
    assert registry is not None
    assert registry.get_schema("start_ask_run") is not None
