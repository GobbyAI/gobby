"""Acceptance coverage for evidence-aware agent self-termination."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_lifecycle_tools import register_agent_lifecycle_tools
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.plans.review_evidence_models import validate_round_result
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore


def _terminal_payload(
    *,
    evidence_id: str = "evidence-1",
    verdict: str = "inconclusive",
    reason: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "verdict": verdict,
        "evidence_id": evidence_id,
        "reason": reason
        or {
            "reason_code": "source_drift",
            "paths": ["src/gobby/plans/review_evidence.py"],
        },
    }


def _make_registry(
    *,
    run: SimpleNamespace,
    evidence: SimpleNamespace | None,
    current_session_id: str | None = "session-1",
    trusted_run_id: str | None = None,
) -> tuple[InternalToolRegistry, MagicMock, MagicMock]:
    runner = MagicMock()
    runner.get_run.return_value = run
    manager = MagicMock()
    manager.get_by_session.return_value = run if current_session_id else None
    manager.db = MagicMock()
    evidence_store = MagicMock()
    evidence_store.get_by_dispatch_run.return_value = evidence
    ctx = AgentsRegistryContext(
        runner=runner,
        agent_run_manager=manager,
        resolve_session_id=lambda value: value,
        get_current_session_id=lambda: current_session_id,
        get_current_agent_run_id=lambda: trusted_run_id,
        get_project_context=lambda: None,
        review_evidence_store=evidence_store,
    )
    registry = InternalToolRegistry(name="test-agents")
    register_agent_lifecycle_tools(registry, ctx)
    return registry, manager, evidence_store


@pytest.mark.asyncio
async def test_end_agent_run_refuses_without_round_result() -> None:
    run = SimpleNamespace(
        id="run-1",
        child_session_id="session-1",
        result=None,
        status="running",
        tmux_session_name=None,
    )
    evidence = SimpleNamespace(evidence_id="evidence-1", is_live=True)
    registry, _manager, store = _make_registry(run=run, evidence=evidence)
    runtime = MagicMock()
    runtime._complete_self_terminated_run = AsyncMock(
        return_value={"success": True, "status": "success"}
    )

    with patch("gobby.mcp_proxy.tools.agents_lifecycle_tools.facade", return_value=runtime):
        missing = await registry.call("end_agent_run", {})
        run.result = "verdict and coverage_attestation"
        malformed = await registry.call("end_agent_run", {})
        run.result = json.dumps(_terminal_payload(evidence_id="evidence-other"))
        mismatched = await registry.call("end_agent_run", {})
        run.result = json.dumps(_terminal_payload())
        completed = await registry.call("end_agent_run", {})

    assert missing["success"] is False
    assert "round result" in missing["error"].lower()
    assert malformed["success"] is False
    assert "round result" in malformed["error"].lower()
    assert mismatched["success"] is False
    assert "evidence-1" in mismatched["error"]
    assert completed == {"success": True, "run_id": "run-1", "status": "success"}
    assert runtime._complete_self_terminated_run.await_count == 1
    assert store.get_by_dispatch_run.call_count == 4


@pytest.mark.asyncio
async def test_end_agent_run_run_identity_fallback_and_spoofing() -> None:
    run = SimpleNamespace(
        id="run-trusted",
        child_session_id="session-1",
        result=None,
        status="running",
        tmux_session_name=None,
    )
    registry, manager, _store = _make_registry(
        run=run,
        evidence=None,
        current_session_id=None,
        trusted_run_id="run-trusted",
    )
    runtime = MagicMock()
    runtime._complete_self_terminated_run = AsyncMock(
        return_value={"success": True, "status": "success"}
    )

    with patch("gobby.mcp_proxy.tools.agents_lifecycle_tools.facade", return_value=runtime):
        with pytest.raises(ValueError, match="Unknown argument.*run_id"):
            await registry.call("end_agent_run", {"run_id": "run-spoofed"})
        result = await registry.call("end_agent_run", {})

    assert result == {"success": True, "run_id": "run-trusted", "status": "success"}
    assert registry._tools["end_agent_run"] is not None
    manager.get_by_session.assert_not_called()


def test_registry_constructor_injects_dependencies() -> None:
    from gobby.utils.session_context import get_current_agent_run_id

    runner = MagicMock()
    runner.run_storage = MagicMock()
    db = MagicMock()
    with (
        patch(
            "gobby.mcp_proxy.tools.agents_registry.register_agent_lifecycle_tools"
        ) as register_lifecycle,
        patch("gobby.mcp_proxy.tools.agents_registry.register_agent_query_tools"),
        patch("gobby.mcp_proxy.tools.agents_registry.register_agent_spawn_tools"),
    ):
        create_agents_registry(runner, db=db)

    ctx = register_lifecycle.call_args.args[1]
    assert isinstance(ctx.review_evidence_store, PlanReviewEvidenceStore)
    assert ctx.review_evidence_store.db is db
    assert ctx.get_current_agent_run_id is get_current_agent_run_id
    assert callable(PlanReviewEvidenceStore.get_by_dispatch_run)


@pytest.mark.asyncio
async def test_verdict_discriminated_terminal_branches() -> None:
    payloads = [
        _terminal_payload(
            verdict="needs_requirements",
            reason={
                "reason_code": "missing_requirements",
                "questions": ["Which runtime owns the retry deadline?"],
            },
        ),
        _terminal_payload(),
        _terminal_payload(
            reason={
                "reason_code": "index_mismatch",
                "expected_token": "generation-1",
                "actual_token": "generation-2",
            }
        ),
        _terminal_payload(
            reason={
                "reason_code": "timeout",
                "timeout_seconds": 900,
            }
        ),
    ]
    evidence = SimpleNamespace(evidence_id="evidence-1", is_live=True)
    runtime = MagicMock()
    runtime._complete_self_terminated_run = AsyncMock(
        return_value={"success": True, "status": "success"}
    )

    with patch("gobby.mcp_proxy.tools.agents_lifecycle_tools.facade", return_value=runtime):
        for payload in payloads:
            assert validate_round_result(payload) == payload
            run = SimpleNamespace(
                id="run-1",
                child_session_id="session-1",
                result=json.dumps(payload),
                status="running",
                tmux_session_name=None,
            )
            registry, _manager, _store = _make_registry(run=run, evidence=evidence)
            result = await registry.call("end_agent_run", {})
            assert result["success"] is True

    assert runtime._complete_self_terminated_run.await_count == len(payloads)
