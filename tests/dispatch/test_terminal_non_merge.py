"""Dispatcher terminal-close contracts for non-merge Phase 5 manifests."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_research_spike_closes_at_prd() -> None:
    source = source_text("src/gobby/dispatch/rules.py")

    assert "research_spike" in source
    assert "prd.done" in source
    assert "manifest_exhausted" in source


def test_prd_doc_closes_at_prd() -> None:
    source = source_text("src/gobby/dispatch/rules.py")

    assert "prd_doc" in source
    assert "prd.done" in source
    assert "manifest_exhausted" in source


def test_architecture_doc_closes_at_architecture() -> None:
    source = source_text("src/gobby/dispatch/rules.py")

    assert "architecture_doc" in source
    assert "architecture.done" in source
    assert "manifest_exhausted" in source


def test_research_spike_at_ideation_with_disabled_placeholder_escalates_with_ideation_no_agent() -> (
    None
):
    source = source_text("src/gobby/dispatch/rules.py")

    assert "disabled_agent_escalation_rule" in source
    for reason in (
        "ideation_no_agent",
        "research_no_agent",
        "architecture_no_agent",
        "prd_no_agent",
    ):
        assert reason in source
