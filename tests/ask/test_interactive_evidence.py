"""Interactive evidence resolves authority before invoking native retrieval."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.mcp_proxy.tools.ask import create_ask_registry
from gobby.utils.session_context import session_context_for_test
from tests.mcp_proxy.tools.test_ask import _project_context


async def test_interactive_evidence_binds_context_and_forwards_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    service = SimpleNamespace(snapshot_manager=SimpleNamespace(snapshot_executable=Path("gcode")))

    def resolve_root(project_id: str, project_path: str | None) -> Path:
        assert project_id == "caller-project"
        assert project_path is None
        return tmp_path

    async def retrieve(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"items": [{"evidence_id": "source-hash"}], "continuation": "page-2"}

    monkeypatch.setattr("gobby.ask.interactive_evidence.retrieve_evidence", retrieve)
    monkeypatch.setattr("gobby.mcp_proxy.tools.ask.get_current_agent_run_id", lambda: None)
    registry = create_ask_registry(lambda _: service, project_root_resolver=resolve_root)
    schema = registry.get_schema("evidence")
    assert schema is not None
    assert set(schema["inputSchema"]["properties"]) == {"operation", "selector", "continuation"}
    with _project_context("caller-project"), session_context_for_test("caller-session"):
        result = await registry.call(
            "evidence",
            {
                "operation": "read",
                "selector": {"kind": "commit_metadata"},
                "continuation": "page-1",
            },
        )
    assert result["items"] == [{"evidence_id": "source-hash"}]
    assert calls == [
        {
            "executable": Path("gcode"),
            "project_root": tmp_path,
            "operation": "read",
            "selector": {"kind": "commit_metadata"},
            "continuation": "page-1",
        }
    ]
    assert not list(tmp_path.iterdir())


async def test_interactive_evidence_rejects_missing_context(tmp_path: Path) -> None:
    registry = create_ask_registry(lambda _: None, project_root_resolver=lambda *_: tmp_path)
    with pytest.raises(RuntimeError, match="project context"):
        await registry.call("evidence", {"operation": "search", "selector": {"query": "x"}})


async def test_managed_ask_cannot_bypass_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str]] = []

    class Permissions:
        def __init__(self, db: object) -> None:
            pass

        def authorize_if_ask(self, agent: str, server: str, tool: str, args: object) -> None:
            calls.append((agent, server, tool))
            raise PermissionError("Ask reviewer cannot call gobby-ask.evidence")

    monkeypatch.setattr("gobby.ask.permissions.AskPermissionStore", Permissions)
    monkeypatch.setattr("gobby.mcp_proxy.tools.ask.get_current_agent_run_id", lambda: "reviewer")
    service = SimpleNamespace(storage=SimpleNamespace(manager=SimpleNamespace(db=object())))
    registry = create_ask_registry(lambda _: service, project_root_resolver=lambda *_: tmp_path)
    with _project_context("caller-project"), session_context_for_test("caller-session"):
        with pytest.raises(PermissionError, match="cannot call"):
            await registry.call("evidence", {"operation": "read", "selector": {"kind": "range"}})
    assert calls == [("reviewer", "gobby-ask", "evidence")]
