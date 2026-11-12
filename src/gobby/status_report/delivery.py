"""Semantic report comparison and delivered full-report baselines."""

import asyncio
import hashlib
import json
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from gobby.status_report.model import StatusData, WorktreeRow
from gobby.storage.hub.protocol import HubDatabase

type Snapshot = dict[str, dict[str, str]]
type ReportMode = Literal["full", "delta"]

_PENDING_SENDS: set[asyncio.Task[dict[str, Any]]] = set()

_SECTIONS = (
    "in_flight",
    "closed",
    "escalated",
    "landings",
    "attention",
    "decisions",
    "stalled",
    "worktrees",
    "idle_holders",
)


@dataclass(frozen=True)
class ReportBaseline:
    run_id: int
    snapshot: Snapshot
    created_at: datetime


class ReportStore:
    def __init__(
        self,
        db: HubDatabase,
        project_id: str,
        machine_id: str,
        destination_key: str,
    ) -> None:
        self.db = db
        self.project_id = project_id
        self.machine_id = machine_id
        self.destination_key = destination_key

    def reserve(self, mode: ReportMode, now: datetime) -> int:
        with self.db.transaction() as conn:
            row = conn.execute(
                "INSERT INTO status_report_runs "
                "(project_id, machine_id, destination_key, mode, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (self.project_id, self.machine_id, self.destination_key, mode, now),
            ).fetchone()
        if row is None:
            raise RuntimeError("Report run reservation returned no identity")
        return int(row["id"])

    def publish(self, run_id: int, path: Path, digest: str, snapshot: Snapshot) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE status_report_runs SET file_path = %s, artifact_digest = %s, "
                "snapshot = %s::jsonb WHERE id = %s AND project_id = %s AND machine_id = %s "
                "AND destination_key = %s AND file_path IS NULL RETURNING id",
                (
                    str(path.resolve()),
                    digest,
                    json.dumps(snapshot),
                    run_id,
                    self.project_id,
                    self.machine_id,
                    self.destination_key,
                ),
            ).fetchone()
        if row is None:
            raise ValueError("Report run is already published or belongs to another scope")

    def capture_artifact(self, path: Path) -> tuple[int, bytes, str] | None:
        row = self.db.fetchone(
            "SELECT id, project_id, machine_id, destination_key, artifact_digest "
            "FROM status_report_runs WHERE file_path = %s",
            (str(path.resolve()),),
        )
        if row is None:
            return None
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if not (
            str(row["project_id"]) == self.project_id
            and str(row["machine_id"]) == self.machine_id
            and row["destination_key"] == self.destination_key
            and row["artifact_digest"] == digest
        ):
            raise ValueError("Report artifact differs from its recorded scope or digest")
        return int(row["id"]), payload, digest

    def acknowledge(self, run_id: int, message_id: str, digest: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE status_report_runs SET delivered_at = COALESCE(delivered_at, now()), "
                "message_id = COALESCE(message_id, %s) "
                "WHERE id = %s AND project_id = %s AND machine_id = %s "
                "AND destination_key = %s AND artifact_digest = %s RETURNING id",
                (
                    message_id,
                    run_id,
                    self.project_id,
                    self.machine_id,
                    self.destination_key,
                    digest,
                ),
            ).fetchone()
        return row is not None

    def baseline(self) -> ReportBaseline | None:
        row = self.db.fetchone(
            "SELECT id, snapshot, created_at FROM status_report_runs "
            "WHERE project_id = %s AND machine_id = %s AND destination_key = %s "
            "AND mode = 'full' AND delivered_at IS NOT NULL ORDER BY id DESC LIMIT 1",
            (self.project_id, self.machine_id, self.destination_key),
        )
        if row is None:
            return None
        snapshot = row["snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        if not isinstance(snapshot, dict) or any(
            not isinstance(key, str)
            or not isinstance(items, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in items.items())
            for key, items in snapshot.items()
        ):
            raise ValueError("Delivered report baseline is malformed")
        return ReportBaseline(int(row["id"]), cast(Snapshot, snapshot), row["created_at"])


async def deliver_artifact(
    store: ReportStore | None,
    path: Path,
    send: Callable[[Path], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    try:
        captured = None if store is None else await asyncio.to_thread(store.capture_artifact, path)
    except (OSError, ValueError):
        return {
            "success": False,
            "error": "Report artifact differs from its recorded scope or digest",
        }
    if captured is None:
        return await send(path)
    run_id, payload, digest = captured

    async def send_snapshot() -> dict[str, Any]:
        # Outbound delivery survives caller cancellation, so its snapshot must too.
        with tempfile.TemporaryDirectory(prefix=".delivery-", dir=path.parent) as directory:
            snapshot = Path(directory) / path.name
            snapshot.write_bytes(payload)
            snapshot.chmod(0o400)
            return await send(snapshot)

    task = asyncio.create_task(send_snapshot())
    _PENDING_SENDS.add(task)

    def finished(completed: asyncio.Task[dict[str, Any]]) -> None:
        _PENDING_SENDS.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(finished)
    result = await asyncio.shield(task)
    if result.get("success") is True:
        message = result.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        if (
            store is None
            or not isinstance(message_id, str)
            or not isinstance(message, dict)
            or message.get("status") != "sent"
            or not await asyncio.to_thread(store.acknowledge, run_id, message_id, digest)
        ):
            return {"success": False, "error": "Report sent but delivery acknowledgement failed"}
    return result


def report_mode(policy: Mapping[str, Any], now: datetime) -> ReportMode:
    matches: list[ReportMode] = []
    for mode, default in (("full", "00-29 UTC"), ("delta", "30-59 UTC")):
        raw = policy.get(f"{mode}_report_minutes", default)
        match = re.fullmatch(r"(\d{1,2})-(\d{1,2})\s+(\S+)", raw) if isinstance(raw, str) else None
        if match is None:
            raise ValueError(f"Invalid {mode} report minute range")
        start, end = int(match[1]), int(match[2])
        if not 0 <= start <= end <= 59:
            raise ValueError(f"Invalid {mode} report minute range")
        minute = now.astimezone(ZoneInfo(match[3])).minute
        if start <= minute <= end:
            matches.append("full" if mode == "full" else "delta")
    if len(matches) != 1:
        raise ValueError("Report schedule must select exactly one mode")
    return matches[0]


def _json_default(value: object) -> str | float:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    raise TypeError(f"Unsupported report snapshot value: {type(value).__name__}")


def _row_snapshot(row: Any) -> str:
    body = asdict(row)
    if isinstance(row, WorktreeRow):
        body.pop("change_at", None)
        note = row.current_note
        body["note"] = (
            None
            if note is None
            else {
                "owner": note.owner,
                "next_action": note.next_action,
                "blocker": note.blocker,
                "eta": note.eta,
            }
        )
    body.pop("idle_seconds", None)
    return json.dumps(body, default=_json_default, sort_keys=True, separators=(",", ":"))


def _row_key(row: Any) -> str:
    parts = [
        str(value)
        for name in (
            "seq",
            "task_seq",
            "branch",
            "session_id",
            "session_ref",
            "run_id",
            "commit_sha",
        )
        if (value := getattr(row, name, None)) is not None
    ]
    return ":".join(parts) if parts else _row_snapshot(row)


def semantic_snapshot(data: StatusData) -> Snapshot:
    result = {
        name: {_row_key(row): _row_snapshot(row) for row in getattr(data, name)}
        for name in _SECTIONS
    }
    result["gaps"] = {gap: gap for gap in data.gaps}
    result["load"] = (
        {"load": json.dumps([data.load_5m, data.load_threshold])}
        if data.load_5m is not None and data.load_5m > data.load_threshold
        else {}
    )
    return result


def delta_data(data: StatusData, baseline: Snapshot, baseline_at: datetime) -> StatusData:
    current = semantic_snapshot(data)
    changes: dict[str, Any] = {
        name: tuple(
            row
            for row in getattr(data, name)
            if current[name][_row_key(row)] != baseline.get(name, {}).get(_row_key(row))
        )
        for name in _SECTIONS
    }
    changes["gaps"] = tuple(gap for gap in data.gaps if gap not in baseline.get("gaps", {}))
    if current["load"] == baseline.get("load", {}):
        changes["load_5m"] = None
    removed = tuple(
        f"{name.replace('_', ' ')}: {key}"
        for name, rows in baseline.items()
        for key in rows
        if key not in current.get(name, {})
    )
    return replace(data, **changes, report_mode="delta", baseline_at=baseline_at, removed=removed)
