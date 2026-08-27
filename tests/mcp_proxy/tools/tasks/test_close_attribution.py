"""Close-checklist attribution capture when session variables have gone empty.

Session variables are a volatile cache of which files a task edited. Escalation,
`_live_session_recovery`, and a fresh claiming session all leave that cache empty
for a task that really did edit files. The task's linked commits are the durable
record, so the checklist must fall back to them rather than treat committed work
as a no-edit close — a no-edit close skips gate 10 entirely, which is exactly the
validation evidence the gate exists to demand.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.config.validation_detection import default_validation_detection_config
from gobby.mcp_proxy.tools.tasks import _lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import capture_attribution
from gobby.storage.session_models import Session
from gobby.storage.tasks import Task
from gobby.tasks.transcript_evidence import derive_transcript_evidence
from gobby.utils.machine_id import get_machine_id

pytestmark = pytest.mark.unit

TASK_ID = "11111111-2222-4333-8444-555555555555"
OWNER_SESSION_ID = "owner-session"
COMMITTED_PATHS = frozenset({"src/gobby/memory/recall.py", "tests/memory/test_recall.py"})


@pytest.fixture
def repo_with_task_commit(tmp_path: Path) -> tuple[str, str]:
    """Return a repository path and the sha of a commit touching `COMMITTED_PATHS`."""

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    for relative in sorted(COMMITTED_PATHS):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n")
        git("add", relative)
    git("commit", "-qm", "task work")
    return str(tmp_path), git("rev-parse", "HEAD")


def _task(*, commits: list[str] | None) -> Task:
    return cast(
        Task,
        SimpleNamespace(
            id=TASK_ID,
            seq_num=20766,
            commits=commits,
            claimed_by_session_id=OWNER_SESSION_ID,
            is_closed=False,
            closed_at=None,
        ),
    )


def _ctx(variables: dict[str, Any]) -> RegistryContext:
    return cast(
        RegistryContext,
        SimpleNamespace(
            session_var_manager=SimpleNamespace(get_variables=lambda _session: variables),
            session_task_manager=SimpleNamespace(get_task_sessions=lambda _task_id: []),
        ),
    )


@pytest.mark.asyncio
async def test_linked_commit_paths_stand_in_for_lost_session_attribution(
    repo_with_task_commit: tuple[str, str],
) -> None:
    """B.4: committed work is never a no-edit close, whatever the session cache says."""
    repo_path, commit_sha = repo_with_task_commit

    snapshot = await capture_attribution(
        _ctx({"task_edited_files": {}}),
        task=_task(commits=[commit_sha]),
        task_id=TASK_ID,
        resolved_session_id="closing-session",
        repo_path=repo_path,
    )

    assert snapshot.raw_paths == COMMITTED_PATHS
    assert snapshot.edited_paths == COMMITTED_PATHS
    assert snapshot.attributed is True
    assert snapshot.had_attributed_edits is True


@pytest.mark.asyncio
async def test_prospective_commit_paths_survive_released_session_attribution(
    repo_with_task_commit: tuple[str, str],
) -> None:
    repo_path, commit_sha = repo_with_task_commit
    commit_union = getattr(close_finalization, "_attribution_commit_shas", None)

    assert callable(commit_union)
    assert commit_union(_task(commits=[commit_sha]), (commit_sha,)) == (commit_sha,)
    assert commit_union(_task(commits=[commit_sha]), ("", commit_sha)) == (commit_sha,)

    snapshot = await capture_attribution(
        _ctx({"task_edited_files": {}}),
        task=_task(commits=None),
        task_id=TASK_ID,
        resolved_session_id="closing-session",
        repo_path=repo_path,
        prospective_commit_shas=(commit_sha,),
    )

    assert snapshot.raw_paths == COMMITTED_PATHS
    assert snapshot.edited_paths == snapshot.raw_paths
    assert snapshot.had_attributed_edits is True

    transcript = Path(repo_path) / "codex.jsonl"
    records = [
        {
            "timestamp": "2026-08-27T03:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "owned-edit",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: src/gobby/memory/recall.py\n",
            },
        },
        {
            "timestamp": "2026-08-27T03:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "unrelated-edit",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: src/gobby/other.py\n",
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    session = cast(
        Session,
        SimpleNamespace(
            id="session-1",
            external_id="external-1",
            machine_id=get_machine_id(),
            source="codex",
            transcript_path=str(transcript),
        ),
    )

    evidence = await derive_transcript_evidence(
        session,
        None,
        default_validation_detection_config(),
        set(snapshot.edited_paths),
        repo_path,
    )

    assert [edit.path for edit in evidence.edits] == ["src/gobby/memory/recall.py"]


@pytest.mark.asyncio
async def test_session_attribution_wins_and_a_commitless_task_is_still_no_edit(
    repo_with_task_commit: tuple[str, str],
) -> None:
    """B.5: the fallback only fills a gap; it never overrides or invents attribution.

    Live session attribution is the more precise record — it names the files this
    task touched rather than everything its commits carried — so it takes
    precedence. A task with neither is a genuine no-edit close and stays one.
    """
    repo_path, commit_sha = repo_with_task_commit

    live = await capture_attribution(
        _ctx({"task_edited_files": {TASK_ID: ["src/gobby/memory/recall.py"]}}),
        task=_task(commits=[commit_sha]),
        task_id=TASK_ID,
        resolved_session_id="closing-session",
        repo_path=repo_path,
    )

    assert live.raw_paths == frozenset({"src/gobby/memory/recall.py"})
    assert live.had_attributed_edits is True

    no_edit = await capture_attribution(
        _ctx({"task_edited_files": {}}),
        task=_task(commits=None),
        task_id=TASK_ID,
        resolved_session_id="closing-session",
        repo_path=repo_path,
    )

    assert no_edit.raw_paths == frozenset()
    assert no_edit.attributed is False
    assert no_edit.had_attributed_edits is False
