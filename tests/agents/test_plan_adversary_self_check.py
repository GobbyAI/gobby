"""Wiring tests for plan-adversary.yaml self-check + retry + fallback (§2.22.4).

After writing the ``## M1 Task Manifest``, the adversary must validate the
plan via ``parse_plan(parse_mode="expansion")``. On failure the adversary
retries up to 3 times. After the cap is exhausted, behavior splits:

  - non-yolo: escalate with ``needs_human:manifest_emission_failure:...``
  - yolo: NEVER escalate; write a ``## Yolo Fallbacks`` audit, fall back to
    ``emit_stub_manifest(plan_path)`` from §2.21a, re-run the strict parse.
    If the stub also fails, append a second audit marker and force-approve
    with ``mark_task_review_approved``.

The pre-verdict review pass uses ``parse_mode="draft"`` so the loop does not
deadlock on a not-yet-written manifest (§2.21.3).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit

ADVERSARY_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/plan-adversary.yaml"
)


@pytest.fixture(scope="module")
def agent() -> AgentDefinitionBody:
    with ADVERSARY_PATH.open() as f:
        data = yaml.safe_load(f)
    return AgentDefinitionBody.model_validate(data)


class TestSelfCheckGate:
    """Post-emission strict self-check uses parse_plan(parse_mode='expansion')."""

    def test_self_check_uses_parse_plan_expansion_mode(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "parse_plan" in instructions
        assert 'parse_mode="expansion"' in instructions or "parse_mode='expansion'" in instructions

    def test_pre_verdict_review_uses_draft_mode(self, agent: AgentDefinitionBody) -> None:
        """Pre-verdict pass uses draft mode so the loop doesn't deadlock on
        not-yet-written manifest (§2.21.3)."""
        instructions = agent.instructions or ""
        assert 'parse_mode="draft"' in instructions or "parse_mode='draft'" in instructions


class TestRetryAndCap:
    def test_retry_capped_at_three(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert re.search(
            r"\b3\s+retr(?:y|ies)\b|retr(?:y|ies)\D{0,15}\b3\b",
            instructions,
            re.IGNORECASE,
        ), "expected explicit '3 retries' cap in adversary instructions"


class TestNonYoloEscalates:
    def test_non_yolo_escalates_with_needs_human_prefix(self, agent: AgentDefinitionBody) -> None:
        """After cap exhausted, non-yolo calls escalate_task with the
        documented manifest-emission-failure prefix."""
        instructions = agent.instructions or ""
        assert "escalate_task" in instructions
        assert "needs_human:manifest_emission_failure" in instructions


class TestYoloFallback:
    """yolo NEVER calls escalate_task on this path (top-level invariant)."""

    def test_yolo_never_escalates_after_cap(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        lowered = instructions.lower()
        assert "yolo" in lowered
        assert "never" in lowered or "do not" in lowered or "do NOT" in instructions
        assert "escalate" in lowered

    def test_yolo_falls_back_to_stub_emitter(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "emit_stub_manifest" in instructions

    def test_yolo_writes_yolo_fallbacks_audit(self, agent: AgentDefinitionBody) -> None:
        instructions = agent.instructions or ""
        assert "Yolo Fallbacks" in instructions

    def test_force_approve_when_stub_also_fails(self, agent: AgentDefinitionBody) -> None:
        """If even the stub-emitter fallback fails, append a second audit
        marker and approve the plan with mark_task_review_approved."""
        instructions = agent.instructions or ""
        assert "force-approve" in instructions or "force_approve" in instructions
        assert "downstream gobby expand will reject" in instructions
