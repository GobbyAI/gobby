"""Reporter source projection stays bounded despite historical digest cells."""

import json

import pytest

from gobby.agents.reporter_sources import digest_projection, task_refs

pytestmark = pytest.mark.unit


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
