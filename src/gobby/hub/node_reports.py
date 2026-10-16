"""Hub-side inbox and workspace recording for node run reports.

The inbox lives in memory. A hub restart loses it, and the new process's epoch
refuses every report built before the restart; workspace rows are already
durable and the node's next fresh report refills the snapshot.
"""

from __future__ import annotations

import functools
import logging
import secrets
import threading
import uuid
from dataclasses import dataclass

from gobby.hub.node_commands import ReportAgent, ReportBody, ReportExit, ReportWorkspace
from gobby.storage.hub.protocol import HubDatabase, Transaction

logger = logging.getLogger(__name__)

_ExitKey = tuple[str, str]


@dataclass(frozen=True)
class TakenReport:
    """A machine's newest installed snapshot and every exit gathered since the last take."""

    snapshot: ReportBody
    exits: tuple[ReportExit, ...]


class NodeReportInbox:
    """Per-machine admission high-waters, published snapshots, and gathered exits."""

    def __init__(self) -> None:
        # The wire carries the epoch as a u64; 63 random bits stay inside every signed reader.
        self.hub_epoch = secrets.randbits(63)
        self._lock = threading.Lock()
        self._high_water: dict[str, int] = {}
        self._published: dict[str, int] = {}
        self._snapshots: dict[str, ReportBody] = {}
        self._exits: dict[str, dict[_ExitKey, ReportExit]] = {}

    def admit(self, machine_id: str, hub_epoch: int | None, revision: int) -> bool:
        """Admit a report from this epoch above the machine's high-water, and raise it."""
        with self._lock:
            if hub_epoch != self.hub_epoch or revision <= self._high_water.get(machine_id, 0):
                return False
            self._high_water[machine_id] = revision
            return True

    def put(self, machine_id: str, report: ReportBody) -> None:
        """Merge the report's exits; install its snapshot only above the published revision."""
        with self._lock:
            exits = self._exits.setdefault(machine_id, {})
            for exit_entry in report.exits:
                exits[(exit_entry.run_id, exit_entry.terminal_id)] = exit_entry
            if report.revision > self._published.get(machine_id, 0):
                self._published[machine_id] = report.revision
                self._snapshots[machine_id] = report

    def take(self) -> dict[str, TakenReport]:
        """Drain each machine with a snapshot installed since the last take."""
        with self._lock:
            taken = {
                machine_id: TakenReport(snapshot, tuple(self._exits.pop(machine_id, {}).values()))
                for machine_id, snapshot in self._snapshots.items()
            }
            self._snapshots.clear()
            return taken


@functools.cache
def node_report_inbox() -> NodeReportInbox:
    return NodeReportInbox()


def record_remote_workspaces(db: HubDatabase, machine_id: str, report: ReportBody) -> None:
    """Record every reported workspace whose terminal is this machine's and names its run."""
    entries: list[ReportExit | ReportAgent] = [*report.exits, *report.agents]
    with db.transaction() as conn:
        for entry in entries:
            if entry.workspace is not None:
                _record_workspace(
                    conn, machine_id, entry.run_id, entry.terminal_id, entry.workspace
                )


def _record_workspace(
    conn: Transaction,
    machine_id: str,
    run_id: str,
    terminal_id: str,
    workspace: ReportWorkspace,
) -> None:
    run = _reported_run(conn, machine_id, run_id, terminal_id)
    if run is None:
        logger.warning(
            "Skipping reported workspace: terminal %r on machine %s does not name run %r",
            terminal_id,
            machine_id,
            run_id,
        )
        return
    table, path_column = (
        ("worktrees", "worktree_path") if workspace.mode == "worktree" else ("clones", "clone_path")
    )
    # Update first: worktrees has a second unique index (branch), so an upsert on the
    # path would abort the report's transaction on a branch conflict.
    moved = conn.execute(
        f"UPDATE {table} SET updated_at = clock_timestamp() "  # nosec B608
        f"WHERE machine_id = %s AND {path_column} = %s",
        (machine_id, workspace.path),
    )
    if moved.rowcount:
        return
    conn.execute(
        f"""
        INSERT INTO {table} (
            id, project_id, machine_id, task_id, branch_name, {path_column},
            base_branch, agent_session_id, status, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', clock_timestamp())
        ON CONFLICT DO NOTHING
        """,  # nosec B608
        (
            str(uuid.uuid4()),
            run["project_id"],
            machine_id,
            run["task_id"],
            workspace.branch_name,
            workspace.path,
            workspace.base_branch,
            run["session_id"],
        ),
    )


def _reported_run(
    conn: Transaction, machine_id: str, run_id: str, terminal_id: str
) -> dict[str, object] | None:
    if not (_is_uuid(run_id) and _is_uuid(terminal_id)):
        return None
    # The session join keeps the (agent_session_id, machine_id) foreign key satisfiable.
    row = conn.execute(
        """
        SELECT terminal.project_id, run.task_id, session.id AS session_id
        FROM terminals AS terminal
        JOIN agent_runs AS run ON run.id = terminal.agent_run_id
        LEFT JOIN sessions AS session
            ON session.id = run.child_session_id AND session.machine_id = terminal.machine_id
        WHERE terminal.id = %s AND terminal.machine_id = %s AND terminal.agent_run_id = %s
        """,
        (terminal_id, machine_id, run_id),
    ).fetchone()
    return dict(row) if row is not None else None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
