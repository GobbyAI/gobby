"""Daemon-attested landing receipts that a task close may cite.

A close criterion can name the landing of an exact commit, which happens in
the lander's session. The closing session may not be the lander, so
land_commit records a receipt here with the lander as its author. Review
approval is the review step record (``step_records``), not a receipt.

Receipts are ``task_comments`` rows under a reserved ``author_type``. The
comment HTTP routes refuse to create or delete that type, so no client can
forge or erase one.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import Task

__all__ = [
    "CLOSE_RECEIPT_AUTHOR_TYPE",
    "LANDING",
    "CloseReceipt",
    "CloseReceiptError",
    "list_close_receipts",
    "record_close_receipt",
]

CLOSE_RECEIPT_AUTHOR_TYPE = "close_receipt"
# Stored rows of a retired kind (activation, landing_approval,
# independent_review_approval) read as nothing.
LANDING = "landing"

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_MAX_FACTS = 16
_MAX_FACT_KEY_CHARS = 64
_MAX_FACT_VALUE_CHARS = 300


class CloseReceiptError(ValueError):
    """A receipt the daemon refuses to record."""


@dataclass(frozen=True)
class CloseReceipt:
    id: str
    task_id: str
    author_session_id: str
    kind: str
    commit_sha: str
    facts: dict[str, str | int | bool]
    recorded_at: str


def record_close_receipt(
    db: HubDatabase,
    *,
    task: Task,
    author_session_id: str,
    commit_sha: str,
    facts: object = None,
) -> tuple[CloseReceipt, bool]:
    """Record a landing receipt, returning a replay with the same author and commit.

    The caller has already verified that ``commit_sha`` names a commit in the
    task's repository. Authority comes from the task row as locked here, so
    ``task`` contributes only its id.
    """
    sha = commit_sha.strip().lower()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise CloseReceiptError("commit_sha must be a full 40-character commit SHA")
    body: dict[str, Any] = {"kind": LANDING, "commit_sha": sha, "facts": _bounded_facts(facts)}

    with db.transaction() as conn:
        # Authorize against the locked row, never the caller's snapshot: the claim
        # can change while the caller awaits commit verification. The lock also
        # serializes receipts per task so a replay cannot insert a duplicate.
        authority = conn.execute(
            """
            SELECT claimed_by_session_id::text AS claimed_by_session_id
            FROM tasks WHERE id = %s FOR UPDATE
            """,
            (task.id,),
        ).fetchone()
        if authority is None:
            raise CloseReceiptError(f"task {task.id} no longer exists")
        if author_session_id == authority["claimed_by_session_id"]:
            raise CloseReceiptError("the task's claimant cannot attest evidence for its own close")
        rows = conn.execute(
            """
            SELECT id, task_id, author, body, created_at FROM task_comments
            WHERE task_id = %s AND author_type = %s AND author = %s
            ORDER BY created_at, id
            """,
            (task.id, CLOSE_RECEIPT_AUTHOR_TYPE, author_session_id),
        ).fetchall()
        for row in rows:
            existing = _receipt_from_row(row)
            if existing is not None and existing.commit_sha == sha:
                return existing, False
        inserted = conn.execute(
            """
            INSERT INTO task_comments (id, task_id, author, author_type, body)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, task_id, author, body, created_at
            """,
            (
                str(uuid.uuid4()),
                task.id,
                author_session_id,
                CLOSE_RECEIPT_AUTHOR_TYPE,
                json.dumps(body, sort_keys=True, separators=(",", ":")),
            ),
        ).fetchone()
    receipt = _receipt_from_row(inserted)
    if receipt is None:
        raise RuntimeError(f"close receipt for task {task.id} was not persisted")
    return receipt, True


def list_close_receipts(db: HubDatabase, task_id: str) -> list[CloseReceipt]:
    rows = db.fetchall(
        """
        SELECT id, task_id, author, body, created_at FROM task_comments
        WHERE task_id = %s AND author_type = %s
        ORDER BY created_at, id
        """,
        (task_id, CLOSE_RECEIPT_AUTHOR_TYPE),
    )
    return [receipt for row in rows if (receipt := _receipt_from_row(row)) is not None]


def _bounded_facts(facts: object) -> dict[str, str | int | bool]:
    if facts is None:
        return {}
    if not isinstance(facts, Mapping):
        raise CloseReceiptError("facts must be an object")
    if len(facts) > _MAX_FACTS:
        raise CloseReceiptError(f"facts may hold at most {_MAX_FACTS} entries")
    bounded: dict[str, str | int | bool] = {}
    for key, value in facts.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_FACT_KEY_CHARS:
            raise CloseReceiptError(
                "each fact key must be a non-empty string of at most "
                f"{_MAX_FACT_KEY_CHARS} characters"
            )
        if not isinstance(value, (str, int, bool)):
            raise CloseReceiptError(f"fact {key!r} must be a string, integer, or boolean")
        if isinstance(value, str) and len(value) > _MAX_FACT_VALUE_CHARS:
            raise CloseReceiptError(f"fact {key!r} exceeds {_MAX_FACT_VALUE_CHARS} characters")
        bounded[key] = value
    return bounded


def _receipt_from_row(row: Mapping[str, Any] | None) -> CloseReceipt | None:
    if row is None:
        return None
    try:
        body = json.loads(str(row["body"]))
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    kind = body.get("kind")
    sha = body.get("commit_sha")
    facts = body.get("facts")
    if kind != LANDING or not isinstance(sha, str) or not isinstance(facts, dict):
        return None
    created_at = row["created_at"]
    return CloseReceipt(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        author_session_id=str(row["author"]),
        kind=str(kind),
        commit_sha=sha,
        facts=facts,
        recorded_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
    )
