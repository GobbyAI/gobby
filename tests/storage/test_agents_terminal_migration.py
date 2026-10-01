"""Storage-package readers resolve through terminals instead of tmux_session_name."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

pytestmark = pytest.mark.unit

_STORAGE_AGENTS = Path(__file__).resolve().parents[2] / "src/gobby/storage/agents"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_storage_readers_use_terminal_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    for path in sorted(_STORAGE_AGENTS.glob("*.py")):
        assert "tmux_session_name" not in path.read_text(encoding="utf-8"), path.name

    session = session_manager.register(
        external_id="terminal-migration-session",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)
    never_started = manager.create(
        parent_session_id=session.id,
        provider="claude",
        prompt="never started",
    )
    started = manager.create(
        parent_session_id=session.id,
        provider="claude",
        prompt="started",
    )
    manager.start(started.id)
    live = make_live_terminal(started, backend="tmux", db=temp_db)
    pending = make_pending_terminal(never_started, backend="tmux", db=temp_db)
    temp_db.execute(
        """
        UPDATE agent_runs
        SET status = 'success', completed_at = now(), updated_at = now()
        WHERE id = %s
        """,
        (started.id,),
    )
    terminal_runs = manager.list_terminal_with_tmux()
    assert started.id in {run.id for run in terminal_runs}
    assert never_started.id not in {run.id for run in terminal_runs}

    from gobby.storage.terminals import TerminalManager

    terminals = TerminalManager(temp_db)
    terminals.mark_exited(live.id)
    after = manager.get(started.id)
    assert after is not None
    assert after.terminal_id == live.id
    assert started.id not in {run.id for run in manager.list_terminal_with_tmux()}
    reloaded = terminals.get(live.id)
    assert reloaded is not None
    assert reloaded.state not in {"pending", "live"}
    pending_row = terminals.get(pending.id)
    assert pending_row is not None
    assert pending_row.state == "pending"


def test_orphaned_terminal_rows_stay_eligible_for_the_sweep(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    """A failed kill leaves the row orphaned; the sweep must retry it, not drop it."""
    from gobby.storage.terminals import TerminalManager

    session = session_manager.register(
        external_id="terminal-orphan-sweep-session",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)
    run = manager.create(
        parent_session_id=session.id,
        provider="claude",
        prompt="orphaned",
    )
    manager.start(run.id)
    terminal = make_live_terminal(run, backend="tmux", db=temp_db)
    temp_db.execute(
        """
        UPDATE agent_runs
        SET status = 'success', completed_at = now(), updated_at = now()
        WHERE id = %s
        """,
        (run.id,),
    )
    terminals = TerminalManager(temp_db)
    terminals.mark_orphaned(terminal.id)
    assert run.id in {row.id for row in manager.list_terminal_with_tmux()}

    terminals.mark_exited(terminal.id)
    assert run.id not in {row.id for row in manager.list_terminal_with_tmux()}


@pytest.mark.parametrize("transition", ["complete", "fail", "timeout", "cancel"])
@pytest.mark.parametrize(("orphaned", "settled_state"), [(False, "exited"), (True, "orphaned")])
def test_run_transition_leaves_an_orphaned_terminal_to_its_settlement(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    transition: str,
    orphaned: bool,
    settled_state: str,
) -> None:
    """An orphaned row's kill is unproven, so ending its run must not mark it exited."""
    from gobby.storage.terminals import TerminalManager

    session = session_manager.register(
        external_id=f"terminal-orphan-transition-{transition}-{orphaned}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)
    run = manager.create(parent_session_id=session.id, provider="claude", prompt="transition")
    manager.start(run.id)
    terminal = make_live_terminal(run, backend="tmux", db=temp_db)
    terminals = TerminalManager(temp_db)
    if orphaned:
        terminals.mark_orphaned(terminal.id)
    ends: dict[str, Callable[[str], object]] = {
        "complete": manager.complete,
        "fail": lambda run_id: manager.fail(run_id, "failed"),
        "timeout": manager.timeout,
        "cancel": manager.cancel,
    }

    assert ends[transition](run.id) is not None

    row = terminals.get(terminal.id)
    assert row is not None
    assert row.state == settled_state
