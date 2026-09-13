"""Constructive enhancement remains advisory and preserves serialized suggestions."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog

pytestmark = pytest.mark.unit
REFERENCE = Path("src/gobby/install/shared/skills/gobby/references/plan/enhancement.md")


@pytest.fixture
def body() -> str:
    return " ".join(REFERENCE.read_text().split())


def test_enhancement_catalog_identity() -> None:
    assert load_capability_catalog().folded_skills["plan-enhance"] == (
        "gobby:references/plan/enhancement.md"
    )


def test_scope_and_conformance_judgment(body: str) -> None:
    for term in (
        "Better strengthens existing scope",
        "Bigger adds scope only when traceable",
        "Preserve any mandated mechanism exactly",
        "Contract silence is Better/clarity",
        "wrong claim is a correctness issue to point out, not redesign",
        "restraint's ladder",
        "proportionality",
        "Record the stopping rung",
        "Rank surviving offers by impact versus effort, then risk",
    ):
        assert term in body


def test_no_mutation_verdict_or_quota_authority(body: str) -> None:
    assert "never edits the plan, approves/rejects, writes M1, or gates correctness" in body
    assert "without quotas" in body
    assert "Converged means an empty list after a complete pass" in body
    assert "this never implies approval of another phase" in body


@pytest.mark.parametrize(
    "field",
    [
        "converged",
        "lens",
        "category",
        "location",
        "description",
        "suggested_enhancement",
        "impact",
        "effort",
        "risk",
        "severity",
        "stable ID",
    ],
)
def test_suggestion_fields_remain_complete(body: str, field: str) -> None:
    assert field in body


def test_closed_payload_values_and_complete_presentation(body: str) -> None:
    for term in (
        "`better` or `bigger`",
        "`scope`, `testability`, `reuse`, `sequencing`, or `clarity`",
        "`low`, `med`, or `high`",
        "`small`, `medium`, or `large`",
        "`severity` is always `opportunity`",
        "every metadata field",
        "do not substitute a summary",
        "rationale for every suggestion",
        "individual accept/decline decisions before edits",
    ):
        assert term in body


def test_completion_uses_supported_tools_and_typed_handoff(body: str) -> None:
    for term in (
        "record_plan_enhancement",
        "suggestions serialized as strings",
        "end_agent_run requires current_state and next_steps",
        "independent of adversarial review",
        "Do not record a failed/truncated run as converged",
    ):
        assert term in body
