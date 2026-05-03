from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TRIAGE_LABELS = {
    "gobby:accepted",
    "gobby:skipped",
    "gobby:duplicate",
    "gobby:needs-triage",
    "gobby:resolved",
}


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_github_issue_triage_doc_has_mermaid_architecture_source() -> None:
    doc = _read("docs/guides/github-issue-triage.md")

    assert "```mermaid" in doc
    for term in (
        "GitHub issues webhook",
        "gh_triage_deliveries",
        "Reconciliation cron",
        "VectorStore/Qdrant",
        "triage-agent + triage-judgment",
        "Create or update linked Gobby task",
        "GitHub comments",
        "GitHub labels",
        "close_linked_github_issue",
    ):
        assert term in doc


def test_github_triage_label_set_matches_docs_skill_and_agent() -> None:
    docs = _read("docs/guides/github-issue-triage.md")
    skill = _read("src/gobby/install/shared/skills/triage-judgment/SKILL.md")
    agent = _read("src/gobby/install/shared/workflows/agents/triage-agent.yaml")

    for label in TRIAGE_LABELS:
        assert label in docs
        assert label in skill
        assert label in agent
    assert "implement|skip|escalate|dedup" in agent
