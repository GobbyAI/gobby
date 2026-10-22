"""Close has no evidence relay; shared stop evidence and historical reads survive."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.tasks.validation_history import ValidationHistoryManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.found_work_gate import unresolved_validation_failures


def test_close_has_no_evidence_dependencies() -> None:
    source = Path(__file__).parents[2] / "src/gobby"
    paths = [source / "tasks/close_invariants.py", source / "tasks/close_checklist.py"]
    paths.extend((source / "mcp_proxy/tools/tasks").glob("_lifecycle_close*.py"))
    forbidden = ("transcript", "validation", "acceptance", "criterion", "close_review")
    imports = [
        (path.name, node.module)
        for path in paths
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and any(part in node.module for part in forbidden)
    ]
    assert imports == []


async def test_shared_transcript_consumers_survive(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    transcript = tmp_path / "session.jsonl"
    records: list[dict[str, Any]] = []
    for index, failed in enumerate((True, False)):
        call_id = f"validation-{index}"
        started = now + timedelta(seconds=index * 2)
        records.extend(
            [
                {
                    "type": "assistant",
                    "timestamp": started.isoformat(),
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": "Bash",
                                "input": {"command": "uv run pytest tests/test_widget.py"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": (started + timedelta(seconds=1)).isoformat(),
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "content": "Exit code 1\n1 failed" if failed else "1 passed",
                                "is_error": failed,
                            }
                        ],
                    },
                },
            ]
        )
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    session = Session(
        id="00000000-0000-0000-0000-000000000001",
        external_id="shared-transcript",
        machine_id=require_machine_id(),
        source="claude",
        project_id="project",
        title=None,
        status="active",
        transcript_path=str(transcript),
        summary_path=None,
        summary_markdown=None,
        git_branch="test",
        parent_session_id=None,
        created_at=now,
        updated_at=now,
    )
    evidence = await derive_transcript_evidence(session, now, default_validation_detection_config())
    assert [run.outcome for run in evidence.validation_runs] == ["failure", "success"]
    assert unresolved_validation_failures(evidence.validation_runs[:1], owner_handoff=False)
    assert unresolved_validation_failures(evidence.validation_runs, owner_handoff=False) == ()


async def test_live_prefix_has_no_close_semantics(
    temp_db: HubDatabase, sample_git_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = LocalTaskManager(temp_db)
    ctx = RegistryContext(task_manager=manager)
    session = ctx.session_manager.register(
        external_id="live-criteria",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_git_project["id"],
    )
    criteria = "Live: activation and smoke testing remain ordinary reviewer instructions."
    task = manager.create_task(
        sample_git_project["id"], "No-work closure", validation_criteria=criteria
    )
    manager.claim_task(task.id, session.id)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: session.id
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, ctx)
    result = await registry.call("close_task", {"task_id": task.id, "reason": "moot"})
    assert result["closed"] is True, result
    closed = manager.get_task(task.id)
    assert closed.validation_criteria == criteria
    assert closed.review_status == "pending" and closed.review_fail_count == 0
    history = ValidationHistoryManager(temp_db)
    assert history.get_iteration_history(task.id) == []
    assert not hasattr(history, "record_iteration")
    assert not hasattr(history, "clear_history")
