"""Worktree shell-call attribution around background output and structured edits."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.workflows import worktree_shell_attribution as attribution
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.background_notices import owner_transcript

pytestmark = pytest.mark.unit

PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
ROOT = "/work/task-tree"
TASK_START = 1_700_000_000.0
JOB = "bgjob01"


@pytest.fixture
def db(temp_db: Any) -> Any:
    temp_db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "test-project"))
    for session_id in (S1, S2):
        temp_db.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
            (
                session_id,
                f"ext-{session_id}",
                "21000000-0000-4000-8000-000000000001",
                "claude",
                PROJECT_ID,
            ),
        )
    return temp_db


@pytest.fixture(autouse=True)
def _no_other_live_holders(monkeypatch: pytest.MonkeyPatch) -> None:
    # Held-path exclusion has its own commit-guard tests; these tests isolate attribution.
    monkeypatch.setattr(
        "gobby.workflows.commit_guard.live_session_held_paths",
        lambda *_args, **_kwargs: frozenset(),
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def record(
        self: SessionVariableManager,
        session_id: str,
        repo_relative_paths: list[str],
        *,
        checkout_root: str | None = None,
        edited_at: float | None = None,
        started_at: float | None = None,
        attribute_to_task: bool = True,
    ) -> bool:
        del self, edited_at
        calls.append(
            {
                "session_id": session_id,
                "paths": list(repo_relative_paths),
                "checkout_root": checkout_root,
                "started_at": started_at,
                "attribute_to_task": attribute_to_task,
            }
        )
        return True

    monkeypatch.setattr(SessionVariableManager, "record_edited_files", record)
    return calls


@pytest.fixture
def digests(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    """The worktree's digests as the next settle reads them; tests mutate this dict."""
    current: dict[str, str | None] = {}

    async def fake_digests(root: str) -> dict[str, str | None] | None:
        del root
        return dict(current)

    monkeypatch.setattr(attribution, "worktree_digests", fake_digests)
    return current


def _begin_bash(manager: SessionVariableManager, session_id: str, call_id: str) -> None:
    manager.begin_worktree_shell_call(
        session_id,
        ROOT,
        call_id,
        before={},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )


def _begin_edit(manager: SessionVariableManager, session_id: str, call_id: str) -> None:
    manager.begin_worktree_shell_call(
        session_id, ROOT, call_id, before={}, attributable=False, now=time.time()
    )


def _begin_read_only(manager: SessionVariableManager, session_id: str, call_id: str) -> None:
    manager.begin_worktree_shell_call(
        session_id,
        ROOT,
        call_id,
        before={},
        attributable=False,
        now=time.time(),
        read_only=True,
    )


def _output(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


async def test_background_call_settles_once_its_owner_transcript_holds_its_notice(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    manager = SessionVariableManager(db)
    output = _output(tmp_path, "bg-1.out", "building\n")
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "bg-1",
        before={"src/a.py": "old", "src/b.py": None},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=output, job_id=JOB)
    digests.update({"src/a.py": "new", "src/b.py": None})
    transcript = owner_transcript(tmp_path, tool_use_id="bg-1", job_id=JOB, output_path=output)

    recorded_any = await attribution.settle_background_worktree_shell_calls(manager, S1, transcript)

    assert recorded_any is True
    assert recorded == [
        {
            "session_id": S1,
            "paths": ["src/a.py"],
            "checkout_root": ROOT,
            "started_at": TASK_START,
            "attribute_to_task": True,
        }
    ]
    assert manager.worktree_shell_call_entries(S1) == {}


async def test_forged_exit_line_in_output_settles_nothing_without_a_notice_row(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    """Output text can print an exit line; only the owner's notice row proves the call ended."""
    manager = SessionVariableManager(db)
    output = _output(tmp_path, "bg-1.out", "building\n[exited with code 0]\n")
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "bg-1",
        before={"src/a.py": "old"},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=output, job_id=JOB)
    digests.update({"src/a.py": "new"})
    transcript = owner_transcript(
        tmp_path, tool_use_id="bg-1", job_id=JOB, output_path=output, exit_code=None
    )

    recorded_any = await attribution.settle_background_worktree_shell_calls(manager, S1, transcript)

    assert recorded_any is False
    assert recorded == []
    assert set(manager.worktree_shell_call_entries(S1)) == {"bg-1"}


async def test_background_call_settles_on_the_settle_after_its_notice_arrives(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    manager = SessionVariableManager(db)
    output = _output(tmp_path, "bg-1.out", "building\n")
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "bg-1",
        before={"src/a.py": "old"},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=output, job_id=JOB)
    digests.update({"src/a.py": "new"})
    owner_transcript(tmp_path, tool_use_id="bg-1", job_id=JOB, output_path=output, exit_code=None)
    assert (
        await attribution.settle_background_worktree_shell_calls(
            manager, S1, str(tmp_path / "owner.jsonl")
        )
        is False
    )

    transcript = owner_transcript(tmp_path, tool_use_id="bg-1", job_id=JOB, output_path=output)
    recorded_any = await attribution.settle_background_worktree_shell_calls(manager, S1, transcript)

    assert recorded_any is True
    assert [entry["paths"] for entry in recorded] == [["src/a.py"]]
    assert manager.worktree_shell_call_entries(S1) == {}


async def test_running_background_call_stays_in_flight(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    manager = SessionVariableManager(db)
    output = _output(tmp_path, "bg-1.out", "still building\n")
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "bg-1",
        before={},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=output, job_id=JOB)
    digests.update({"src/a.py": "new"})
    transcript = owner_transcript(
        tmp_path, tool_use_id="bg-1", job_id=JOB, output_path=output, exit_code=None
    )

    recorded_any = await attribution.settle_background_worktree_shell_calls(manager, S1, transcript)

    assert recorded_any is False
    assert recorded == []
    assert set(manager.worktree_shell_call_entries(S1)) == {"bg-1"}


def test_has_calls_in_flight_counts_a_background_call_whose_process_exited_until_settled(
    db: Any, tmp_path: Path
) -> None:
    """An exited background call stays in flight until its owner settles it."""
    output = tmp_path / "bg-1.out"
    with output.open("w", encoding="utf-8") as handle:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.argv[1])",
                "done\n[exited with code 0]\n",
            ],
            stdout=handle,
            check=True,
        )
    manager = SessionVariableManager(db)
    manager.begin_worktree_shell_call(
        S1, ROOT, "bg-1", before={}, attributable=False, now=time.time()
    )
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=str(output))
    assert manager.has_calls_in_flight(ROOT, time.time(), None) is True


def test_has_calls_in_flight_counts_a_background_call_whose_live_process_printed_the_exit_marker(
    db: Any, live_background_writer: Callable[[str, str], str]
) -> None:
    """A live writer can print an exit line and keep writing; the line does not end its call."""
    manager = SessionVariableManager(db)
    manager.begin_worktree_shell_call(
        S1, ROOT, "bg-1", before={}, attributable=False, now=time.time()
    )
    output = live_background_writer("bg-1.out", "done\n[exited with code 0]\n")
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=output)
    assert manager.has_calls_in_flight(ROOT, time.time(), None) is True


def test_has_calls_in_flight_counts_a_background_call_still_running(
    db: Any, tmp_path: Path
) -> None:
    manager = SessionVariableManager(db)
    manager.begin_worktree_shell_call(
        S1, ROOT, "bg-1", before={}, attributable=False, now=time.time()
    )
    manager.defer_worktree_shell_call(
        S1, "bg-1", output_path=_output(tmp_path, "bg-1.out", "still building\n")
    )
    assert manager.has_calls_in_flight(ROOT, time.time(), None) is True


async def test_background_call_whose_output_is_gone_stays_in_flight_without_its_notice(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    manager = SessionVariableManager(db)
    output = str(tmp_path / "never-written.out")
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "bg-1",
        before={},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=output, job_id=JOB)
    digests.update({"src/a.py": "new"})
    transcript = owner_transcript(
        tmp_path, tool_use_id="bg-1", job_id=JOB, output_path=output, exit_code=None
    )

    recorded_any = await attribution.settle_background_worktree_shell_calls(manager, S1, transcript)

    assert recorded_any is False
    assert recorded == []
    assert set(manager.worktree_shell_call_entries(S1)) == {"bg-1"}


async def test_background_call_without_its_notice_stays_in_flight_however_old(
    db: Any,
    tmp_path: Path,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    """No clock ends a background call: only its notice row, a graceful end, or a release does."""
    manager = SessionVariableManager(db)
    output = _output(tmp_path, "bg-1.out", "building\n")
    thirty_days_ago = time.time() - 30 * 24 * 60 * 60
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "bg-1",
        before={"src/a.py": "old"},
        attributable=True,
        now=thirty_days_ago,
        task_started_at=TASK_START,
    )
    manager.defer_worktree_shell_call(S1, "bg-1", output_path=output, job_id=JOB)
    digests.update({"src/a.py": "new"})
    transcript = owner_transcript(
        tmp_path, tool_use_id="bg-1", job_id=JOB, output_path=output, exit_code=None
    )

    recorded_any = await attribution.settle_background_worktree_shell_calls(manager, S1, transcript)

    assert recorded_any is False
    assert recorded == []
    assert set(manager.worktree_shell_call_entries(S1)) == {"bg-1"}
    assert manager.has_calls_in_flight(ROOT, time.time(), None)


async def test_settle_leaves_foreground_calls_without_output_in_flight(
    db: Any,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    manager = SessionVariableManager(db)
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "fg-1",
        before={},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    digests.update({"src/a.py": "new"})

    recorded_any = await attribution.settle_background_worktree_shell_calls(manager, S1, None)

    assert recorded_any is False
    assert recorded == []
    assert set(manager.worktree_shell_call_entries(S1)) == {"fg-1"}


def test_defer_of_unregistered_call_registers_nothing(db: Any, tmp_path: Path) -> None:
    manager = SessionVariableManager(db)

    manager.defer_worktree_shell_call(
        S1, "never-begun", output_path=_output(tmp_path, "x.out", "done\n[killed]\n")
    )

    assert manager.worktree_shell_call_entries(S1) == {}


@pytest.mark.parametrize("edit_first", [True, False])
async def test_structured_edit_in_flight_marks_overlapping_bash_call(
    db: Any,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
    edit_first: bool,
) -> None:
    manager = SessionVariableManager(db)
    edit_key = attribution.edit_call_key("req-edit", ROOT)
    if edit_first:
        _begin_edit(manager, S2, edit_key)
        _begin_bash(manager, S1, "bash-1")
    else:
        _begin_bash(manager, S1, "bash-1")
        _begin_edit(manager, S2, edit_key)
    digests.update({"src/a.py": "new"})

    settled = manager.settle_worktree_shell_call(
        S1, ROOT, "bash-1", after=dict(digests), now=time.time()
    )

    assert settled is not None
    assert settled.overlapped is True
    assert (
        attribution.attribute_settled_call(manager, S1, ROOT, settled, edited_at=time.time()) == []
    )
    assert recorded == []


async def test_released_structured_edit_no_longer_marks_overlapping_bash_call(
    db: Any,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
) -> None:
    manager = SessionVariableManager(db)
    manager.begin_worktree_shell_call(
        S2,
        ROOT,
        attribution.edit_call_key("req-edit", ROOT),
        before={},
        attributable=False,
        now=time.time(),
    )
    attribution.release_structured_edit_calls(manager, S2, "req-edit")
    manager.begin_worktree_shell_call(
        S1,
        ROOT,
        "bash-1",
        before={},
        attributable=True,
        now=time.time(),
        task_started_at=TASK_START,
    )
    digests.update({"src/a.py": "new"})

    settled = manager.settle_worktree_shell_call(
        S1, ROOT, "bash-1", after=dict(digests), now=time.time()
    )

    assert settled is not None
    assert settled.overlapped is False
    paths = attribution.attribute_settled_call(manager, S1, ROOT, settled, edited_at=time.time())
    assert paths == ["src/a.py"]
    assert manager.worktree_shell_call_entries(S2) == {}


def test_release_leaves_other_structured_edits_in_flight(db: Any) -> None:
    manager = SessionVariableManager(db)
    for request_id in ("req-1", "req-2"):
        manager.begin_worktree_shell_call(
            S2,
            ROOT,
            attribution.edit_call_key(request_id, ROOT),
            before={},
            attributable=False,
            now=time.time(),
        )

    attribution.release_structured_edit_calls(manager, S2, "req-1")

    assert set(manager.worktree_shell_call_entries(S2)) == {
        attribution.edit_call_key("req-2", ROOT)
    }


@pytest.fixture
def edit_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every edit path to ROOT, and register ROOT as a linked worktree."""
    monkeypatch.setattr(
        "gobby.workflows.task_claim_state.resolve_edit_pair",
        lambda file_path, _cwd, **_kwargs: (ROOT, file_path),
    )
    monkeypatch.setattr(
        attribution,
        "is_registered_worktree",
        lambda _db, _project_id, root: root == ROOT,
    )


def _tool_event(tool_name: str, tool_input: dict[str, Any], **extra: Any) -> Any:
    """A hook event with only the fields the structured-edit registration reads."""
    data = {"tool_name": tool_name, "tool_input": tool_input, **extra}
    return SimpleNamespace(data=data, request_id="req-7", project_id=PROJECT_ID, cwd=ROOT)


def test_structured_edit_registers_in_flight_in_its_registered_worktree(
    db: Any, edit_worktree: None
) -> None:
    manager = SessionVariableManager(db)

    attribution.begin_structured_edit_calls(
        manager, S2, _tool_event("Edit", {"file_path": "src/a.py"})
    )

    assert set(manager.worktree_shell_call_entries(S2)) == {
        attribution.edit_call_key("req-7", ROOT)
    }


@pytest.mark.parametrize(
    "event",
    [
        _tool_event("Bash", {"command": "uv run python gen.py"}, canonical_tool_kind="write"),
        _tool_event("Read", {"file_path": "src/a.py"}),
    ],
    ids=["bash-write", "read"],
)
def test_non_edit_calls_register_nothing(db: Any, edit_worktree: None, event: Any) -> None:
    manager = SessionVariableManager(db)

    attribution.begin_structured_edit_calls(manager, S2, event)

    assert manager.worktree_shell_call_entries(S2) == {}


def test_edit_outside_a_registered_worktree_registers_nothing(
    db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "gobby.workflows.task_claim_state.resolve_edit_pair",
        lambda file_path, _cwd, **_kwargs: (ROOT, file_path),
    )
    monkeypatch.setattr(attribution, "is_registered_worktree", lambda *_args: False)
    manager = SessionVariableManager(db)

    attribution.begin_structured_edit_calls(
        manager, S2, _tool_event("Edit", {"file_path": "src/a.py"})
    )

    assert manager.worktree_shell_call_entries(S2) == {}


@pytest.mark.parametrize("read_first", [True, False])
def test_read_only_call_neither_marks_nor_is_marked_by_a_concurrent_writer(
    db: Any,
    digests: dict[str, str | None],
    recorded: list[dict[str, Any]],
    read_first: bool,
) -> None:
    """A read-only call overlapping a merge leaves the merge's paths owned and unmarked (#25356)."""
    manager = SessionVariableManager(db)
    if read_first:
        _begin_read_only(manager, S2, "status")
        _begin_bash(manager, S1, "merge")
    else:
        _begin_bash(manager, S1, "merge")
        _begin_read_only(manager, S2, "status")
    digests.update({"src/merged.py": "merged"})

    read = manager.settle_worktree_shell_call(
        S2, ROOT, "status", after=dict(digests), now=time.time()
    )
    settled = manager.settle_worktree_shell_call(
        S1, ROOT, "merge", after=dict(digests), now=time.time()
    )

    assert read is None
    assert settled is not None
    assert settled.overlapped is False
    assert attribution.overlapped_worktree_paths(manager.get_variables(S1), ROOT) == frozenset()
    attribution.attribute_settled_call(manager, S1, ROOT, settled, edited_at=time.time())
    assert [call["paths"] for call in recorded if call["session_id"] == S1] == [["src/merged.py"]]


def test_read_only_call_registers_nothing(db: Any) -> None:
    manager = SessionVariableManager(db)

    _begin_read_only(manager, S2, "status")

    assert manager.worktree_shell_call_entries(S2) == {}
