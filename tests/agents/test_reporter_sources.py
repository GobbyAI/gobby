"""Reporter source projection stays bounded despite historical digest cells."""

import json
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo

import pytest

from gobby.agents import reporter_sources
from gobby.agents.reporter_sources import digest_projection, task_refs

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("has_project", "worktree"), [(True, False), (True, True), (False, False)])
def test_main_reads_only_checkout_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    has_project: bool,
    worktree: bool,
) -> None:
    class PinnedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls(2026, 9, 27, tzinfo=tz)

    project = tmp_path / "checkout"
    cwd = project / "nested"
    cwd.mkdir(parents=True)
    report = project / ".gobby/reports/gobby-digest-2026-09-27.md"
    if has_project:
        report.parent.mkdir(parents=True)
        context = {"project_path": str(project)}
        if worktree:
            context["parent_project_path"] = str(tmp_path / "parent")
        (project / ".gobby/project.json").write_text(json.dumps(context))
        report.write_text("## Decisions needed (Josh)\nProject decision evidence\n")
    home = tmp_path / "home"
    desktop = home / "Desktop/gobby-digest-2026-09-27.md"
    desktop.parent.mkdir(parents=True)
    desktop.write_text("## Decisions needed (Josh)\nDesktop decoy evidence\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("sys.argv", ["reporter_sources", "--section", "decisions"])
    monkeypatch.setattr(reporter_sources, "datetime", PinnedDatetime)

    reporter_sources.main()
    result = json.loads(capsys.readouterr().out)
    assert result["section"] == "decisions"
    if has_project:
        assert result["source"] == str(report)
        assert "error" not in result
        assert "Project decision evidence" in json.dumps(result["evidence"])
        assert "Desktop decoy" not in json.dumps(result)
    else:
        assert "project root" in result["error"].lower()
        assert "evidence" not in result
        assert result.get("source") != str(desktop)


def test_current_queues_exclude_history_and_expand_ranges_in_recorded_order() -> None:
    content = (
        "## Previous-day summary — 2026-10-11\nOld #900\n"
        "## Lane queues — current\n"
        "| Lane | Epic | Worktree | Seats and work | Queued next | Parked |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        f"| 1 Runbooks | #22691 Runbooks | lane-1 | {'old #800 ' * 40000} "
        "| #24087–#24092; gobby#16332; #24089 | #24145 Reporter |\n"
        "| 1B | #22691 Runbooks | lane-1-2 | old #700 | #24160 Queued | — |\n"
        "## Standing rules recorded 2026-10-11\n#600 old rule\n"
    )
    result = digest_projection(content, "queues")
    assert result["lanes"] == [
        {
            "lane": "1 Runbooks",
            "epic": "#22691 Runbooks",
            "worktree": "lane-1",
            "queue_refs": [f"#{number}" for number in range(24087, 24093)],
            "parked_refs": ["#24145"],
        },
        {
            "lane": "1B",
            "epic": "#22691 Runbooks",
            "worktree": "lane-1-2",
            "queue_refs": ["#24160"],
            "parked_refs": [],
        },
    ]
    assert len(json.dumps(result).encode()) < 15_000


def test_fleet_uses_latest_topic_evidence_and_excludes_earlier_terminal_states() -> None:
    content = (
        "## Orchestrator updates — 2026-10-12\n"
        "- Close backlog 14/10 at 09:00\n- Close backlog 8/10 at 10:00\n"
        "- Close backlog 7/10 at 11:00\n- Consolidator review at 10:45\n"
        "- Heavy slot 2 released at 11:02\n"
        "## Terminal and daemon status — 2026-10-12\n"
        "- DAEMON BACK at 11:03. Earlier: HOLD and heavy slot 4 at 09:00\n"
        "## Standing rules recorded 2026-10-11\n- HOLD stale\n"
    )
    result = digest_projection(content, "fleet")
    assert result["backlog"] == ["- Close backlog 8/10 at 10:00", "- Close backlog 7/10 at 11:00"]
    assert result["consolidator"] == ["- Consolidator review at 10:45"]
    assert result["heavy_slots"] == ["- Heavy slot 2 released at 11:02"]
    assert result["holds"] == ["- DAEMON BACK at 11:03."]


def test_task_refs_rejects_unbounded_range() -> None:
    with pytest.raises(ValueError, match="exceeds bounded resolution"):
        task_refs("#1–#1000")


def test_decisions_keep_complete_bullets_and_heading_identity() -> None:
    projection = digest_projection(
        "## Decisions needed (Josh)\n- First decision\n  continued evidence\n"
        "- Second decision\n## Operator actions (Josh)\n1. Restart\n"
        "## Gaps and contradictions\n* Missing receipt\n",
        "decisions",
    )
    assert projection["entries"] == [
        {"heading": "Decisions needed (Josh)", "text": "- First decision\n  continued evidence"},
        {"heading": "Decisions needed (Josh)", "text": "- Second decision"},
        {"heading": "Operator actions (Josh)", "text": "1. Restart"},
        {"heading": "Gaps and contradictions", "text": "* Missing receipt"},
    ]


@pytest.mark.parametrize("section", ["decisions", "queues", "fleet"])
def test_main_pages_complete_entries_under_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    section: str,
) -> None:
    project = tmp_path / "checkout"
    reports = project / ".gobby/reports"
    reports.mkdir(parents=True)
    today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    text = "é" * 3000
    if section == "decisions":
        content = "## Decisions needed (Josh)\n" + "".join(f"- {i} {text}\n" for i in range(7))
        expected = [f"- {i} {text}" for i in range(7)] + ["missing heading", "missing heading"]
        key = "entries"
    elif section == "queues":
        content = "## Lane queues — current\n" + "".join(
            f"| {i} | epic | {text} | ignored | #1 | #2 |\n" for i in range(1, 8)
        )
        expected = [str(i) for i in range(1, 8)]
        key = "lanes"
    else:
        content = "## Orchestrator updates — current\n" + "".join(
            f"- {topic} {text}\n" for topic in ("close backlog", "heavy slot", "hold", "ROADMAP")
        )
        expected = []
        key = "backlog"
    (reports / f"gobby-digest-{today}.md").write_text(content)
    monkeypatch.setattr(reporter_sources, "find_project_root", lambda: project)
    offset = 0
    seen: list[str] = []
    counts: list[int] = []
    for _ in range(20):
        monkeypatch.setattr(
            "sys.argv", ["reporter_sources", "--section", section, "--offset", str(offset)]
        )
        reporter_sources.main()
        output = capsys.readouterr().out
        assert len(output.encode()) < 14_000
        page = json.loads(output)
        assert "error" not in page
        assert page["offset"] == offset
        assert page["stale"] is False
        counts.append(page["count"])
        if section == "decisions":
            seen.extend(entry["text"] for entry in page["evidence"][key])
        elif section == "queues":
            seen.extend(entry["lane"] for entry in page["evidence"][key])
        else:
            seen.extend(entry for values in page["evidence"].values() for entry in values)
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    else:
        pytest.fail("Source paging failed to terminate")
    assert len(counts) > 1
    assert sum(counts) == page["total"]
    if section == "fleet":
        assert seen == [
            f"- {topic} {text}" for topic in ("close backlog", "heavy slot", "hold", "ROADMAP")
        ]
    else:
        assert seen == expected


@pytest.mark.parametrize("current_exists", [True, False])
def test_main_rollover_uses_latest_prior_digest_and_labels_age(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current_exists: bool,
) -> None:
    class PinnedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls(2026, 10, 13, tzinfo=tz)

    reports = tmp_path / ".gobby/reports"
    reports.mkdir(parents=True)
    dates = ["2026-10-10", "2026-10-12", "2026-10-14", "invalid"]
    if current_exists:
        dates.append("2026-10-13")
    for day in dates:
        (reports / f"gobby-digest-{day}.md").write_text(f"## Decisions needed (Josh)\n- {day}\n")
    monkeypatch.setattr(reporter_sources, "datetime", PinnedDatetime)
    monkeypatch.setattr(reporter_sources, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["reporter_sources", "--section", "decisions"])
    reporter_sources.main()
    page = json.loads(capsys.readouterr().out)
    source_date = "2026-10-13" if current_exists else "2026-10-12"
    assert page["source"] == str(reports / f"gobby-digest-{source_date}.md")
    assert page["source_date"] == source_date
    assert page["age_days"] == (0 if current_exists else 1)
    assert page["stale"] is (not current_exists)
    assert page["evidence"]["entries"][0]["text"] == f"- {source_date}"


@pytest.mark.parametrize("offset,large", [(-1, False), (0, True)])
def test_source_page_errors_are_bounded_and_identify_the_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    offset: int,
    large: bool,
) -> None:
    reports = tmp_path / ".gobby/reports"
    reports.mkdir(parents=True)
    today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    (reports / f"gobby-digest-{today}.md").write_text(
        "## Decisions needed (Josh)\n- " + "é" * (10_000 if large else 1)
    )
    monkeypatch.setattr(reporter_sources, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["reporter_sources", "--section", "decisions", "--offset", str(offset)]
    )
    reporter_sources.main()
    output = capsys.readouterr().out
    assert len(output.encode()) < 14_000
    result = json.loads(output)
    assert "evidence" not in result
    assert result["error"] == (
        "Digest entry at offset 0 exceeds the 14KB page budget"
        if large
        else "offset must be nonnegative"
    )
