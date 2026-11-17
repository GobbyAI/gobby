"""Durable terminal resource rows and CAS lifecycle transitions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.terminal_locators import (
    FRAMES_SOCKET_NAME as FRAMES_SOCKET_NAME,
)
from gobby.storage.terminal_locators import (
    AttachLocator as AttachLocator,
)
from gobby.storage.terminal_locators import (
    HostEpochMismatchError as HostEpochMismatchError,
)
from gobby.storage.terminal_locators import (
    MachineOwnershipMismatchError as MachineOwnershipMismatchError,
)
from gobby.storage.terminal_locators import (
    TerminalLocatorRecord as TerminalLocatorRecord,
)
from gobby.storage.terminal_locators import (
    TerminalNotReadyError as TerminalNotReadyError,
)
from gobby.storage.terminal_locators import (
    _native_locator as _native_locator,
)
from gobby.storage.terminal_locators import (
    attach_locator_for_terminal as attach_locator_for_terminal,
)
from gobby.storage.terminal_locators import (
    native_attach_locator as native_attach_locator,
)
from gobby.storage.terminal_locators import (
    native_locator_key as native_locator_key,
)
from gobby.storage.terminal_reap_queries import TerminalReapQueryMixin
from gobby.storage.terminal_settlement import (
    ALLOWED_EDGES as ALLOWED_EDGES,
)
from gobby.storage.terminal_settlement import (
    TERMINAL_COLUMNS,
    TerminalSettlementMixin,
    terminal_columns,
)
from gobby.storage.terminal_settlement import (
    UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES as UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES,
)
from gobby.storage.terminal_settlement import (
    UNRESOLVED_WRITE_MAX_ENTRIES as UNRESOLVED_WRITE_MAX_ENTRIES,
)
from gobby.storage.terminal_settlement import (
    UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES as UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES,
)
from gobby.storage.terminal_settlement import (
    IllegalTerminalTransitionError as IllegalTerminalTransitionError,
)
from gobby.storage.terminal_settlement import (
    UnresolvedWriteCapacityError as UnresolvedWriteCapacityError,
)
from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES, recorded_process_is_alive
from gobby.utils.datetime import normalize_datetime_model
from gobby.utils.machine_id import require_machine_id

TITLE_MAX_BYTES = 1024
TERMINAL_STATES = ("pending", "live", "exited", "orphaned")


class ProjectOwnershipConflictError(RuntimeError):
    """Raised when an active locator is already owned by another project."""

    def __init__(self) -> None:
        super().__init__("Active locator is owned by another project")


def truncate_title(title: str | None) -> str | None:
    """Truncate a title to TITLE_MAX_BYTES on a UTF-8 code-point boundary."""
    if title is None:
        return None
    encoded = title.encode("utf-8")
    if len(encoded) <= TITLE_MAX_BYTES:
        return title
    cut = TITLE_MAX_BYTES
    while cut > 0:
        try:
            return encoded[:cut].decode("utf-8")
        except UnicodeDecodeError:
            cut -= 1
    return ""


def _as_str(value: object) -> str:
    return str(value)


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed: object = json.loads(value)
        if parsed is None:
            return None
        if isinstance(parsed, Mapping):
            return dict(parsed)
        raise TypeError(f"expected mapping, got {type(parsed)!r}")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"expected mapping, got {type(value)!r}")


@normalize_datetime_model(
    required=("created_at", "updated_at", "attempt_started_at"),
    optional=("liveness_at", "automatic_write_quarantined_at"),
)
@dataclass
class Terminal:
    """One terminals-table row."""

    id: str
    backend: str
    ownership: str
    state: str
    machine_id: str
    project_id: str
    created_at: datetime
    updated_at: datetime
    attempt_generation: int
    attempt_started_at: datetime
    unresolved_writes: dict[str, Any]
    spawn_key: str | None = None
    locator: dict[str, Any] | None = None
    locator_key: str | None = None
    title: str | None = None
    host_epoch: str | None = None
    automatic_write_quarantined_at: datetime | None = None
    automatic_write_quarantine_action_key: str | None = None
    process: dict[str, Any] | None = None
    rows: int | None = None
    cols: int | None = None
    session_id: str | None = None
    agent_run_id: str | None = None
    liveness_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Terminal:
        """Create a Terminal from a database row."""
        unresolved = _as_mapping(row["unresolved_writes"]) or {}
        return cls(
            id=_as_str(row["id"]),
            backend=_as_str(row["backend"]),
            ownership=_as_str(row["ownership"]),
            state=_as_str(row["state"]),
            machine_id=_as_str(row["machine_id"]),
            project_id=_as_str(row["project_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            attempt_generation=int(row["attempt_generation"] or 1),
            attempt_started_at=row["attempt_started_at"],
            unresolved_writes=dict(unresolved),
            spawn_key=_as_optional_str(row["spawn_key"]),
            locator=_as_mapping(row["locator"]),
            locator_key=_as_optional_str(row["locator_key"]),
            title=_as_optional_str(row["title"]),
            host_epoch=_as_optional_str(row["host_epoch"]),
            automatic_write_quarantined_at=row["automatic_write_quarantined_at"],
            automatic_write_quarantine_action_key=_as_optional_str(
                row["automatic_write_quarantine_action_key"]
            ),
            process=_as_mapping(row["process"]),
            rows=None if row["rows"] is None else int(row["rows"]),
            cols=None if row["cols"] is None else int(row["cols"]),
            session_id=_as_optional_str(row["session_id"]),
            agent_run_id=_as_optional_str(row["agent_run_id"]),
            liveness_at=row["liveness_at"],
        )


def mint_terminal_id() -> str:
    """Mint a terminal row identity; runtimes never generate their own."""
    return str(uuid4())


class TerminalManager(TerminalReapQueryMixin, TerminalSettlementMixin):
    """Hub-transaction CRUD and CAS transitions for the terminals table."""

    def __init__(self, db: HubDatabase) -> None:
        super().__init__()
        self.db = db

    def get(self, terminal_id: str) -> Terminal | None:
        """Load a terminal row by id."""
        row = self.db.fetchone(
            f"SELECT {TERMINAL_COLUMNS} FROM terminals WHERE id = %s", (str(UUID(terminal_id)),)
        )
        return None if row is None else Terminal.from_row(row)

    def get_many(self, terminal_ids: Sequence[str]) -> dict[str, Terminal]:
        """Load terminal rows in one hub query, keyed by durable id."""
        if not terminal_ids:
            return {}
        ids = [str(UUID(terminal_id)) for terminal_id in dict.fromkeys(terminal_ids)]
        rows = self.db.fetchall(
            f"SELECT {TERMINAL_COLUMNS} FROM terminals WHERE id = ANY(%s)", (ids,)
        )
        terminals = (Terminal.from_row(row) for row in rows)
        return {terminal.id: terminal for terminal in terminals}

    def get_by_identity(self, terminal_id: str, spawn_key: str) -> Terminal | None:
        """Load a row only when both durable identity fields match."""
        row = self.get(terminal_id)
        if row is None or row.spawn_key != spawn_key:
            return None
        return row

    def create_pending(
        self,
        terminal_id: str,
        project_id: str,
        backend: str,
        ownership: str,
        spawn_key: str,
        *,
        machine_id: str | None = None,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        rows: int | None = None,
        cols: int | None = None,
        title: str | None = None,
    ) -> Terminal:
        """Insert a pre-effect gobby-owned pending row with no locator."""
        resolved_machine = machine_id or require_machine_id()
        row = self.db.fetchone(
            f"""
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id,
                session_id, agent_run_id, rows, cols, title
            ) VALUES (
                %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING {TERMINAL_COLUMNS}
            """,
            (
                str(UUID(terminal_id)),
                backend,
                ownership,
                spawn_key,
                str(UUID(resolved_machine)),
                str(UUID(project_id)),
                None if session_id is None else str(UUID(session_id)),
                None if agent_run_id is None else str(UUID(agent_run_id)),
                rows,
                cols,
                truncate_title(title),
            ),
        )
        if row is None:
            raise RuntimeError(f"Failed to insert pending terminal {terminal_id}")
        return Terminal.from_row(row)

    def promote_to_live(
        self,
        terminal_id: str,
        *,
        locator: Mapping[str, object],
        locator_key: str,
        host_epoch: str | None = None,
        title: str | None = None,
    ) -> Terminal | None:
        """CAS pending → live while filling physical identity in one statement."""
        row = self.get(terminal_id)
        if row is None:
            return None
        stored_locator: dict[str, object] = dict(_native_locator(locator).items())
        return self._cas(
            terminal_id,
            expected="pending",
            new_state="live",
            extra="""
                , locator = %s
                , locator_key = %s
                , host_epoch = %s
                , title = COALESCE(%s, title)
            """,
            extra_params=(
                Jsonb(dict(stored_locator)),
                locator_key,
                host_epoch,
                truncate_title(title),
            ),
        )

    def transition_for_test(
        self, terminal_id: str, expected: str, new_state: str
    ) -> Terminal | None:
        """Invoke the private CAS with an arbitrary edge for allowlist tests."""
        return self._cas(terminal_id, expected=expected, new_state=new_state)

    def list_by_project(self, project_id: str) -> list[Terminal]:
        """List terminals owned by a project."""
        rows = self.db.fetchall(
            f"SELECT {TERMINAL_COLUMNS} FROM terminals WHERE project_id = %s"
            " ORDER BY created_at ASC",
            (str(UUID(project_id)),),
        )
        return [Terminal.from_row(row) for row in rows]

    def list_page(
        self,
        project_ids: Sequence[str] | None = None,
        *,
        machine_id: str | None = None,
        states: tuple[str, ...] | None = None,
        backend: str | None = None,
        cursor_created_at: datetime | None = None,
        cursor_id: str | None = None,
        limit: int = 100,
    ) -> tuple[list[Terminal], bool]:
        """Stable (created_at, id) page for REST and WS inventory.

        Each of ``project_ids`` and ``machine_id`` narrows the page when given.
        """
        filters: list[str] = []
        params: list[object] = []
        if project_ids is not None:
            filters.append("project_id = ANY(%s)")
            params.append([str(UUID(project_id)) for project_id in project_ids])
        if machine_id is not None:
            filters.append("machine_id = %s")
            params.append(str(UUID(machine_id)))
        if states:
            filters.append("state = ANY(%s)")
            params.append(list(states))
        if backend:
            filters.append("backend = %s")
            params.append(backend)
        if cursor_created_at is not None and cursor_id is not None:
            filters.append("(created_at, id) > (%s, %s)")
            params.extend([cursor_created_at, str(UUID(cursor_id))])
        params.append(limit + 1)
        rows = self.db.fetchall(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            {"WHERE " + " AND ".join(filters) if filters else ""}
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            tuple(params),
        )
        items = [Terminal.from_row(row) for row in rows]
        has_more = len(items) > limit
        if has_more:
            items = items[:limit]
        return items, has_more

    def list_orphaned_by_epoch(self, host_epoch: str) -> list[Terminal]:
        """Orphaned native rows for a vanished host epoch."""
        rows = self.db.fetchall(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            WHERE state = 'orphaned' AND host_epoch = %s
            """,
            (host_epoch,),
        )
        return [Terminal.from_row(row) for row in rows]

    def list_live_by_epoch(self, host_epoch: str) -> list[Terminal]:
        """Live native rows for a host epoch."""
        rows = self.db.fetchall(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            WHERE state = 'live' AND host_epoch = %s
            """,
            (host_epoch,),
        )
        return [Terminal.from_row(row) for row in rows]

    def native_epoch_machine_ids(self, host_epoch: str) -> set[str]:
        """Machines whose native rows, in any state, are bound to a host epoch."""
        rows = self.db.fetchall(
            """
            SELECT DISTINCT machine_id::text AS machine_id FROM terminals
            WHERE backend = 'native' AND host_epoch = %s
            """,
            (host_epoch,),
        )
        return {_as_str(row["machine_id"]) for row in rows}

    def get_live_for_session(self, session_id: str) -> Terminal | None:
        """Newest pending or live terminal linked to a session."""
        row = self.db.fetchone(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            WHERE session_id = %s AND state IN ('pending', 'live')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (str(UUID(session_id)),),
        )
        return None if row is None else Terminal.from_row(row)

    def list_for_session(self, session_id: str) -> list[Terminal]:
        """All terminal evidence for a session, including settled and orphaned rows."""
        rows = self.db.fetchall(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            WHERE session_id = %s
            ORDER BY updated_at DESC, id DESC
            """,
            (str(UUID(session_id)),),
        )
        return [Terminal.from_row(row) for row in rows]

    def resolve_live_for_session(self, session: Session) -> Terminal | None:
        """Resolve a bound row or an eligible row named by terminal context."""
        bound = self.get_live_for_session(session.id)
        if bound is not None:
            return bound
        if session.session_type != "terminal" or not isinstance(session.terminal_context, dict):
            return None

        terminal_id = session.terminal_context.get("gobby_terminal_id")
        if not isinstance(terminal_id, str):
            return None
        try:
            terminal_id = str(UUID(terminal_id))
        except ValueError:
            return None
        terminal = self.get(terminal_id)
        if (
            terminal is None
            or terminal.state not in {"pending", "live"}
            or terminal.project_id != session.project_id
            or terminal.agent_run_id is not None
            or terminal.session_id not in {None, session.id}
        ):
            return None
        return terminal

    def bind_session(self, terminal_id: str, session_id: str, project_id: str) -> Terminal | None:
        """Bind a CLI session to the terminal it started in, or refuse with None.

        Agent terminals are bound at spawn. Only a ``terminal`` session of the
        row's project binds, and a row bound to another session rebinds only
        once that session is no longer live or its recorded CLI
        process is gone, so a nested CLI never takes its parent's pane. The
        terminal id comes from the pane environment, so a malformed one refuses.
        """
        try:
            terminal_id = str(UUID(terminal_id))
        except ValueError:
            return None
        previous = self.db.fetchone(
            """
            SELECT s.* FROM terminals t JOIN sessions s ON s.id = t.session_id
            WHERE t.id = %s AND t.session_id <> %s
            """,
            (terminal_id, str(UUID(session_id))),
        )
        previous_session_id = None
        if previous is not None:
            holder = Session.from_row(previous)
            if holder.status in TERMINAL_OWNER_STATUSES and recorded_process_is_alive(holder):
                return None
            previous_session_id = holder.id
        row = self.db.fetchone(
            f"""
            UPDATE terminals t
            SET session_id = s.id, updated_at = now()
            FROM sessions s
            WHERE t.id = %s AND s.id = %s
              AND t.agent_run_id IS NULL
              AND t.project_id = %s
              AND t.state IN ('pending', 'live')
              AND s.session_type = 'terminal'
              AND (t.session_id IS NOT DISTINCT FROM %s OR t.session_id = s.id)
            RETURNING {terminal_columns("t")}
            """,
            (
                terminal_id,
                str(UUID(session_id)),
                str(UUID(project_id)),
                previous_session_id,
            ),
        )
        if row is None:
            return None
        self.db.execute(
            """
            UPDATE terminals
            SET session_id = NULL, updated_at = now()
            WHERE session_id = %s AND id <> %s
              AND ownership = 'gobby' AND agent_run_id IS NULL
              AND state NOT IN ('pending', 'live')
            """,
            (str(UUID(session_id)), terminal_id),
        )
        return Terminal.from_row(row)

    def release_session(self, terminal_id: str, session_id: str) -> Terminal | None:
        """Unbind an ended session from a gobby-owned terminal with no agent run.

        The terminal outlives the CLI, so the row stays live for the next
        session that starts in it; a row already rebound is left alone.
        """
        row = self.db.fetchone(
            f"""
            UPDATE terminals
            SET session_id = NULL, updated_at = now()
            WHERE id = %s AND session_id = %s
              AND ownership = 'gobby' AND agent_run_id IS NULL
            RETURNING {TERMINAL_COLUMNS}
            """,
            (str(UUID(terminal_id)), str(UUID(session_id))),
        )
        return None if row is None else Terminal.from_row(row)

    def list_live_by_machine(self, machine_id: str) -> list[Terminal]:
        """Live and pending terminals on a machine."""
        rows = self.db.fetchall(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            WHERE machine_id = %s AND state IN ('pending', 'live')
            ORDER BY created_at ASC
            """,
            (str(UUID(machine_id)),),
        )
        return [Terminal.from_row(row) for row in rows]

    def list_reconcilable_by_machine(self, machine_id: str) -> list[Terminal]:
        """Pending, live, and orphaned native-host candidates on a machine."""
        rows = self.db.fetchall(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            WHERE machine_id = %s AND state IN ('pending', 'live', 'orphaned')
            ORDER BY created_at ASC
            """,
            (str(UUID(machine_id)),),
        )
        return [Terminal.from_row(row) for row in rows]

    def attach_locator(
        self,
        terminal_id: str,
        *,
        live_host_epoch: str,
        socket_dir: Path | str,
    ) -> AttachLocator:
        """Compute a request-time attach handle; native path is never stored."""
        row = self.get(terminal_id)
        if row is None:
            raise KeyError(terminal_id)
        return attach_locator_for_terminal(
            row,
            live_host_epoch=live_host_epoch,
            socket_dir=socket_dir,
        )

    def set_dims(self, terminal_id: str, rows: int, cols: int) -> Terminal | None:
        """Record the PTY geometry the runtime was last told to apply."""
        row = self.db.fetchone(
            f"""
            UPDATE terminals
            SET rows = %s, cols = %s, updated_at = now()
            WHERE id = %s
            RETURNING {TERMINAL_COLUMNS}
            """,
            (rows, cols, str(UUID(terminal_id))),
        )
        return None if row is None else Terminal.from_row(row)

    def set_automatic_write_quarantine(self, terminal_id: str, action_key: str) -> Terminal:
        """Set both quarantine columns together."""
        row = self.db.fetchone(
            f"""
            UPDATE terminals
            SET automatic_write_quarantined_at = now(),
                automatic_write_quarantine_action_key = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING {TERMINAL_COLUMNS}
            """,
            (action_key, str(UUID(terminal_id))),
        )
        if row is None:
            raise KeyError(terminal_id)
        return Terminal.from_row(row)

    def clear_automatic_write_quarantine(self, terminal_id: str) -> Terminal:
        """Clear both quarantine columns together."""
        row = self.db.fetchone(
            f"""
            UPDATE terminals
            SET automatic_write_quarantined_at = NULL,
                automatic_write_quarantine_action_key = NULL,
                updated_at = now()
            WHERE id = %s
            RETURNING {TERMINAL_COLUMNS}
            """,
            (str(UUID(terminal_id)),),
        )
        if row is None:
            raise KeyError(terminal_id)
        return Terminal.from_row(row)
