"""A paused seat's pane holds the seat only under the running terminal host's epoch.

Rows left behind by a stopped host keep their old state, so judging liveness by state
alone refuses a dead seat. These cases run against the hub database so the terminal
CHECK constraints shape the rows the way a real daemon writes them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.mcp_proxy.tools.spawn_agent._resume import resolve_resume_target
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager, native_locator_key

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000003"
PRIOR_EPOCH = "host-epoch-before-stop"
CURRENT_EPOCH = "host-epoch-after-restart"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _paused_seat(
    db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    *,
    backend: str,
    state: str,
    epoch: str | None,
) -> str:
    """A paused seat whose run was cancelled, with one terminal row in ``state``."""
    root = session_manager.register(
        external_id=f"resume-epoch-root-{uuid.uuid4().hex[:8]}",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    seat = session_manager.register(
        external_id=f"resume-epoch-{uuid.uuid4().hex[:8]}",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project_id,
        parent_session_id=root.id,
        agent_depth=1,
    )
    runs = LocalAgentRunManager(db)
    run = runs.create(
        parent_session_id=root.id,
        provider="claude",
        prompt="work",
        agent_name="developer",
        child_session_id=seat.id,
    )
    assert runs.cancel(run.id) is not None
    assert session_manager.update_terminal_pickup_metadata(seat.id, agent_run_id=run.id)
    session_manager.update_status(seat.id, "paused")

    terminals = TerminalManager(db)
    terminal_id = str(uuid.uuid4())
    assert terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend=backend,
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=MACHINE_ID,
        session_id=seat.id,
    )
    if state == "live":
        if backend == "native":
            host_terminal_id = f"ht-{terminal_id[:8]}"
            promoted = terminals.promote_to_live(
                terminal_id,
                locator={"host_terminal_id": host_terminal_id},
                locator_key=native_locator_key(epoch or "", host_terminal_id),
                host_epoch=epoch,
            )
        else:
            promoted = terminals.promote_to_live(
                terminal_id,
                locator={"pane_id": "%12"},
                locator_key=f"tmux-{terminal_id}",
            )
        assert promoted is not None
    return seat.id


@pytest.mark.parametrize(
    ("backend", "state", "row_epoch", "live_epoch", "resumes"),
    [
        ("native", "live", PRIOR_EPOCH, CURRENT_EPOCH, True),
        ("native", "pending", None, CURRENT_EPOCH, True),
        ("native", "live", CURRENT_EPOCH, CURRENT_EPOCH, False),
        ("native", "live", PRIOR_EPOCH, None, False),
        ("tmux", "live", None, CURRENT_EPOCH, False),
    ],
    ids=[
        "prior-epoch-live-pane-resumes",
        "pending-row-resumes",
        "current-epoch-live-pane-refuses",
        "unknown-epoch-refuses",
        "tmux-pane-refuses-by-state",
    ],
)
def test_paused_seat_liveness_follows_the_running_host_epoch(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    backend: str,
    state: str,
    row_epoch: str | None,
    live_epoch: str | None,
    resumes: bool,
) -> None:
    project_id = str(sample_project["id"])
    seat_id = _paused_seat(
        temp_db,
        session_manager,
        project_id,
        backend=backend,
        state=state,
        epoch=row_epoch,
    )
    run_storage = LocalAgentRunManager(temp_db)
    if resumes:
        resumed = resolve_resume_target(
            session_manager,
            seat_id,
            project_id,
            None,
            run_storage,
            "developer",
            live_host_epoch=live_epoch,
        )
        assert resumed.id == seat_id
    else:
        with pytest.raises(ValueError, match=f"Resume target {seat_id} is still live"):
            resolve_resume_target(
                session_manager,
                seat_id,
                project_id,
                None,
                run_storage,
                "developer",
                live_host_epoch=live_epoch,
            )
