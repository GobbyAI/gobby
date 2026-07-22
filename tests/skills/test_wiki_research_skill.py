"""Contract tests for the bundled wiki-research review backlog."""

from pathlib import Path

from gobby.skills.parser import parse_skill_file

SKILL_PATH = Path("src/gobby/install/shared/skills/wiki-research/SKILL.md")


def test_backlog_entry_contract_is_detailed_and_idempotent() -> None:
    """Every kept finding gets a stable, append-only review entry."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    assert parsed.name == "wiki-research"
    section_8 = body.split("## 8. Review backlog (always)")[1].split("## 9.")[0]
    normalized = " ".join(section_8.split())

    assert "knowledge/topics/wiki-research-backlog.md" in section_8
    assert "wiki-research-backlog:knowledge/topics/<topic>.md#<finding-slug>" in section_8
    assert "wiki-research-topic:knowledge/topics/<topic>.md#<finding-slug>" in section_8
    assert "- Status: pending review" in section_8
    assert "- Topic:" in section_8
    assert "- Rationale:" in section_8
    assert "- Investigation prompt:" in section_8
    assert "- Citations:" in section_8
    assert "leave the entire entry unchanged" in section_8
    assert "Check the backlog marker and topic marker independently" in section_8
    assert "Preserve the compiled page's source evidence" in normalized


def test_investigation_tasks_require_opt_in_and_link_to_backlog() -> None:
    """Step 9 gates task filing and keeps the backlog canonical."""
    body = parse_skill_file(SKILL_PATH).content
    section_9 = body.split("## 9. Investigation tasks (only when `create_tasks=true`)")[1]
    section_9 = section_9.split("## 10.")[0]
    normalized = " ".join(section_9.split())

    # Task-graph dedup: BM25 search surfaces open AND closed duplicates.
    assert "`search_tasks` on `gobby-tasks`" in section_9
    assert "do not re-file; its close rationale is the answer" in section_9
    # Prior-art check against the codebase.
    assert "gcode search" in section_9
    assert "gcode search-content" in section_9
    # Near-misses are filed with cross-references, not dropped.
    assert "related: #NNNN" in section_9
    assert "exact backlog anchor" in section_9
    assert "backlog entry remains canonical" in section_9
    assert "per surviving item" in section_9
    assert "Skipped filings are visible, never silent." in normalized


def test_run_report_records_triaged_away_items() -> None:
    """Step 10 records backlog and optional-task outcomes."""
    parsed = parse_skill_file(SKILL_PATH)
    body = parsed.content

    section_10 = body.split("## 10. Run report")[1].split("## 11.")[0]
    normalized = " ".join(section_10.split())
    assert "items triaged away in step 9" in normalized
    assert "duplicate task refs or prior-art reasons" in normalized
    assert "backlog path" in section_10
    assert "finding count" in section_10
    assert "`wiki_ingest`" in section_10
    assert "local path" in section_10
    assert "vault source under `raw/`" in section_10
    assert "`outputs/**` reserved for generated artifacts" in section_10
    assert "`wiki_compile` records" in section_10
