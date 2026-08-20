"""Durable terminal resource rows and CAS lifecycle transitions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model, utc_now
from gobby.utils.machine_id import require_machine_id

TITLE_MAX_BYTES = 1024
UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES = 256
UNRESOLVED_WRITE_MAX_ENTRIES = 32
UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES = 65536
FRAMES_SOCKET_NAME = "gterm-frames.sock"
TERMINAL_STATES = ("pending", "live", "exited", "orphaned")
ALLOWED_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        ("pending", "live"),
        ("pending", "exited"),
        ("live", "exited"),
        ("live", "orphaned"),
        ("orphaned", "exited"),
    }
)


class IllegalTerminalTransitionError(RuntimeError):
    """Raised when a caller requests a state edge outside the allowlist."""


class ProjectOwnershipConflictError(RuntimeError):
    """Raised when an active locator is already owned by another project."""

    def __init__(self) -> None:
        super().__init__("Active locator is owned by another project")


class HostEpochMismatchError(RuntimeError):
    """Raised when a native attach's live host epoch does not match the row."""


class UnresolvedWriteCapacityError(RuntimeError):
    """Raised when an unresolved-write latch would exceed durable bounds."""

    def __init__(self) -> None:
        super().__init__("unresolved_write_capacity")


def tmux_locator_key(
    *,
    socket_path: str,
    server_pid: int,
    server_start_time: int,
    pane_id: str,
) -> str:
    """Canonical tmux identity: socket + server generation + pane id."""
    return f"tmux:{socket_path}:{server_pid}:{server_start_time}:{pane_id}"


def native_locator_key(host_epoch: str, host_terminal_id: str) -> str:
    """Canonical native identity: host epoch + host terminal id."""
    return f"native:{host_epoch}:{host_terminal_id}"


def parse_tmux_generation(raw: str) -> dict[str, object]:
    """Parse one display-message expansion of socket, pid, start_time, pane id."""
    socket_path, pid, start_time, pane_id = raw.split("|", 3)
    return {
        "socket_path": socket_path,
        "server_pid": int(pid),
        "server_start_time": int(start_time),
        "pane_id": pane_id,
    }


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


@dataclass(frozen=True)
class AttachLocator:
    """Request-time attach handle; native socket path is computed, never stored."""

    backend: Literal["tmux", "native"]
    frame_host_epoch: str
    host_socket: str | None = None
    host_terminal_id: str | None = None
    socket_path: str | None = None
    pane_id: str | None = None


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
    session_name: str | None = None
    window_id: str | None = None
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
            session_name=_as_optional_str(row["session_name"]),
            window_id=_as_optional_str(row["window_id"]),
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


def _native_locator(locator: Mapping[str, object]) -> dict[str, str]:
    if "host_socket" in locator:
        raise ValueError("native locator must not include host_socket")
    extra = set(locator) - {"host_terminal_id"}
    if extra:
        raise ValueError("native locator may only contain host_terminal_id")
    return {"host_terminal_id": str(locator["host_terminal_id"])}


def _serialized_unresolved_size(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


class TerminalManager:
    """Hub-transaction CRUD and CAS transitions for the terminals table."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def get(self, terminal_id: str) -> Terminal | None:
        """Load a terminal row by id."""
        row = self.db.fetchone("SELECT * FROM terminals WHERE id = %s", (str(UUID(terminal_id)),))
        return None if row is None else Terminal.from_row(row)

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
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id,
                session_id, agent_run_id, rows, cols, title
            ) VALUES (
                %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING *
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
        session_name: str | None = None,
        window_id: str | None = None,
        title: str | None = None,
    ) -> Terminal | None:
        """CAS pending → live while filling physical identity in one statement."""
        row = self.get(terminal_id)
        if row is None:
            return None
        stored_locator: dict[str, object] = (
            dict(_native_locator(locator).items()) if row.backend == "native" else dict(locator)
        )
        return self._cas(
            terminal_id,
            expected="pending",
            new_state="live",
            extra="""
                , locator = %s
                , locator_key = %s
                , host_epoch = %s
                , session_name = COALESCE(%s, session_name)
                , window_id = COALESCE(%s, window_id)
                , title = COALESCE(%s, title)
            """,
            extra_params=(
                Jsonb(dict(stored_locator)),
                locator_key,
                host_epoch,
                session_name,
                window_id,
                truncate_title(title),
            ),
        )

    def fail_pending(self, terminal_id: str) -> Terminal | None:
        """CAS pending → exited for a spawn that never produced a resource."""
        return self._cas(terminal_id, expected="pending", new_state="exited")

    def mark_exited(self, terminal_id: str) -> Terminal | None:
        """CAS live|orphaned → exited without clearing locator identity."""
        live = self._cas(terminal_id, expected="live", new_state="exited")
        if live is not None:
            return live
        return self._cas(terminal_id, expected="orphaned", new_state="exited")

    def mark_orphaned(self, terminal_id: str) -> Terminal | None:
        """CAS live → orphaned (native host-epoch / host-crash loss)."""
        return self._cas(terminal_id, expected="live", new_state="orphaned")

    def transition_for_test(
        self, terminal_id: str, expected: str, new_state: str
    ) -> Terminal | None:
        """Invoke the private CAS with an arbitrary edge for allowlist tests."""
        return self._cas(terminal_id, expected=expected, new_state=new_state)

    def upsert_external(
        self,
        *,
        machine_id: str | None = None,
        project_id: str,
        backend: str,
        locator: Mapping[str, object],
        locator_key: str,
        session_name: str | None = None,
        window_id: str | None = None,
        title: str | None = None,
        host_epoch: str | None = None,
        session_id: str | None = None,
    ) -> Terminal:
        """Idempotent discovery insert/update for an already-running pane."""
        resolved_machine = machine_id or require_machine_id()
        stored_locator: dict[str, object] = (
            dict(_native_locator(locator).items()) if backend == "native" else dict(locator)
        )
        terminal_id = str(uuid4())
        row = self.db.fetchone(
            """
            INSERT INTO terminals (
                id, backend, ownership, state, spawn_key, machine_id, project_id,
                locator, locator_key, session_name, window_id, title, host_epoch,
                session_id, liveness_at
            ) VALUES (
                %s, %s, 'external', 'live', NULL, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, now()
            )
            ON CONFLICT (locator_key) WHERE locator_key IS NOT NULL
                AND state = ANY (ARRAY['pending'::text, 'live'::text])
            DO UPDATE SET
                session_name = EXCLUDED.session_name,
                window_id = EXCLUDED.window_id,
                title = EXCLUDED.title,
                liveness_at = now(),
                updated_at = now()
            WHERE terminals.project_id = EXCLUDED.project_id
            RETURNING *
            """,
            (
                terminal_id,
                backend,
                str(UUID(resolved_machine)),
                str(UUID(project_id)),
                Jsonb(dict(stored_locator)),
                locator_key,
                session_name,
                window_id,
                truncate_title(title),
                host_epoch,
                None if session_id is None else str(UUID(session_id)),
            ),
        )
        if row is not None:
            return Terminal.from_row(row)
        occupying = self.db.fetchone(
            """
            SELECT project_id FROM terminals
            WHERE locator_key = %s AND state IN ('pending', 'live')
            """,
            (locator_key,),
        )
        if occupying is not None and str(occupying["project_id"]) != str(UUID(project_id)):
            raise ProjectOwnershipConflictError()
        raised = self.db.fetchone(
            """
            SELECT * FROM terminals
            WHERE locator_key = %s AND state IN ('pending', 'live')
            """,
            (locator_key,),
        )
        if raised is None:
            raise RuntimeError("upsert_external conflict without occupying row")
        return Terminal.from_row(raised)

    def list_stale_pending(self, max_age_seconds: float) -> list[Terminal]:
        """Pending rows whose attempt_started_at is older than the in-doubt deadline."""
        rows = self.db.fetchall(
            """
            SELECT * FROM terminals
            WHERE state = 'pending'
              AND attempt_started_at <= now() - (%s * INTERVAL '1 second')
            ORDER BY attempt_started_at ASC
            """,
            (max_age_seconds,),
        )
        return [Terminal.from_row(row) for row in rows]

    def list_by_project(self, project_id: str) -> list[Terminal]:
        """List terminals owned by a project."""
        rows = self.db.fetchall(
            "SELECT * FROM terminals WHERE project_id = %s ORDER BY created_at ASC",
            (str(UUID(project_id)),),
        )
        return [Terminal.from_row(row) for row in rows]

    def list_orphaned_by_epoch(self, host_epoch: str) -> list[Terminal]:
        """Orphaned native rows for a vanished host epoch."""
        rows = self.db.fetchall(
            """
            SELECT * FROM terminals
            WHERE state = 'orphaned' AND host_epoch = %s
            """,
            (host_epoch,),
        )
        return [Terminal.from_row(row) for row in rows]

    def list_live_by_epoch(self, host_epoch: str) -> list[Terminal]:
        """Live native rows for a host epoch."""
        rows = self.db.fetchall(
            """
            SELECT * FROM terminals
            WHERE state = 'live' AND host_epoch = %s
            """,
            (host_epoch,),
        )
        return [Terminal.from_row(row) for row in rows]

    def list_live_by_machine(self, machine_id: str) -> list[Terminal]:
        """Live and pending terminals on a machine."""
        rows = self.db.fetchall(
            """
            SELECT * FROM terminals
            WHERE machine_id = %s AND state IN ('pending', 'live')
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
        if row.backend == "native":
            if row.host_epoch != live_host_epoch:
                raise HostEpochMismatchError("Live host epoch does not match the terminal row")
            host_terminal_id = None
            if row.locator is not None:
                host_terminal_id = str(row.locator["host_terminal_id"])
            return AttachLocator(
                backend="native",
                frame_host_epoch=str(row.host_epoch),
                host_socket=str(Path(socket_dir) / FRAMES_SOCKET_NAME),
                host_terminal_id=host_terminal_id,
            )
        return AttachLocator(
            backend="tmux",
            frame_host_epoch=live_host_epoch,
            socket_path=None if row.locator is None else str(row.locator.get("socket_path")),
            pane_id=None if row.locator is None else str(row.locator.get("pane_id")),
        )

    def revalidate_tmux_generation(
        self,
        terminal_id: str,
        live_locator: Mapping[str, object],
    ) -> Terminal | None:
        """Bind only when pid+start_time still match; otherwise mark_exited."""
        row = self.get(terminal_id)
        if row is None or row.locator is None:
            return None
        stored_pid = row.locator.get("server_pid")
        stored_start = row.locator.get("server_start_time")
        if stored_pid != live_locator.get("server_pid") or stored_start != live_locator.get(
            "server_start_time"
        ):
            self.mark_exited(terminal_id)
            return None
        return row

    def record_process(self, terminal_id: str, process: Mapping[str, object]) -> Terminal | None:
        """Store native {pgid, start_time} on a still-pending row."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET process = %s, updated_at = now()
            WHERE id = %s AND state = 'pending' AND backend = 'native'
            RETURNING *
            """,
            (Jsonb(dict(process)), str(UUID(terminal_id))),
        )
        return None if row is None else Terminal.from_row(row)

    def bump_attempt_generation(self, terminal_id: str) -> Terminal | None:
        """Increment attempt_generation and refresh attempt_started_at together."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET attempt_generation = attempt_generation + 1,
                attempt_started_at = now(),
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (str(UUID(terminal_id)),),
        )
        return None if row is None else Terminal.from_row(row)

    def persist_unresolved_write(
        self,
        terminal_id: str,
        action_key: str,
        origin: str,
        *,
        at: datetime | None = None,
    ) -> Terminal:
        """Write-ahead latch one action_key, enforcing durable map bounds."""
        if (
            not action_key
            or len(action_key.encode("utf-8")) > UNRESOLVED_WRITE_ACTION_KEY_MAX_BYTES
        ):
            raise UnresolvedWriteCapacityError()
        current = self.get(terminal_id)
        if current is None:
            raise KeyError(terminal_id)
        writes = dict(current.unresolved_writes)
        if action_key not in writes and len(writes) >= UNRESOLVED_WRITE_MAX_ENTRIES:
            raise UnresolvedWriteCapacityError()
        writes[action_key] = {
            "at": (at or utc_now()).isoformat(),
            "origin": origin,
        }
        if _serialized_unresolved_size(writes) > UNRESOLVED_WRITE_MAX_SERIALIZED_BYTES:
            raise UnresolvedWriteCapacityError()
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET unresolved_writes = %s, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (Jsonb(writes), str(UUID(terminal_id))),
        )
        if row is None:
            raise KeyError(terminal_id)
        return Terminal.from_row(row)

    def set_automatic_write_quarantine(self, terminal_id: str, action_key: str) -> Terminal:
        """Set both quarantine columns together."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET automatic_write_quarantined_at = now(),
                automatic_write_quarantine_action_key = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (action_key, str(UUID(terminal_id))),
        )
        if row is None:
            raise KeyError(terminal_id)
        return Terminal.from_row(row)

    def clear_automatic_write_quarantine(self, terminal_id: str) -> Terminal:
        """Clear both quarantine columns together."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET automatic_write_quarantined_at = NULL,
                automatic_write_quarantine_action_key = NULL,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (str(UUID(terminal_id)),),
        )
        if row is None:
            raise KeyError(terminal_id)
        return Terminal.from_row(row)

    def _cas(
        self,
        terminal_id: str,
        *,
        expected: str,
        new_state: str,
        extra: str = "",
        extra_params: tuple[object, ...] = (),
    ) -> Terminal | None:
        if (expected, new_state) not in ALLOWED_EDGES:
            raise IllegalTerminalTransitionError(
                f"Illegal terminal transition {expected}->{new_state}"
            )
        params = (new_state, *extra_params, str(UUID(terminal_id)), expected)
        row = self.db.fetchone(
            f"""
            UPDATE terminals
            SET state = %s,
                updated_at = now()
                {extra}
            WHERE id = %s AND state = %s
            RETURNING *
            """,
            params,
        )
        return None if row is None else Terminal.from_row(row)
