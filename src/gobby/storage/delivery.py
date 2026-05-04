"""Task delivery state storage.

This module owns PR and merge delivery metadata for a task. It deliberately
keeps delivery state out of the task row and the generic task_artifacts table.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


class DeliveryStateError(RuntimeError):
    """Raised when delivery state storage cannot verify a write."""


CAMPAIGN_COLUMNS = frozenset(
    {
        "state",
        "merge_strategy",
        "structured_pr_verdict",
        "pr_report_ref",
        "merge_sha",
        "merge_report_ref",
        "last_error",
    }
)
UNIT_COLUMNS = frozenset(
    {
        "worktree_id",
        "repo",
        "source_branch",
        "target_branch",
        "pr_required",
        "protection_json",
        "pr_url",
        "github_pr_number",
        "gate_snapshot_json",
        "pr_state",
        "local_update_attempts",
        "last_error",
    }
)
CAMPAIGN_JSON_COLUMNS = frozenset({"structured_pr_verdict"})
UNIT_JSON_COLUMNS = frozenset({"protection_json", "gate_snapshot_json"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _encode_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _decode_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        preview = value[:80].replace("\n", "\\n")
        logger.warning("Malformed delivery JSON ignored: %s; preview=%r", exc, preview)
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


class TaskDeliveryStateManager:
    """Persistence helper for PR/merge delivery state."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def record_campaign(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Upsert campaign-level delivery fields for a task."""
        cleaned = self._clean_campaign_fields(fields)
        values: dict[str, Any] = {
            "task_id": task_id,
            "updated_at": _now(),
            **cleaned,
        }
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column} = excluded.{column}" for column in columns if column != "task_id"
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO task_delivery_campaigns ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(task_id) DO UPDATE SET {updates}
                """,  # nosec B608 - columns are filtered against static allowlists.
                tuple(values[column] for column in columns),
            )
            result = conn.execute(
                """
                SELECT task_id, state, merge_strategy, structured_pr_verdict,
                       pr_report_ref, merge_sha, merge_report_ref, last_error,
                       created_at, updated_at
                  FROM task_delivery_campaigns
                 WHERE task_id = ?
                """,
                (task_id,),
            )
            row = result.fetchone() if result is not None else None
        if row is None:
            raise DeliveryStateError(f"Failed to record delivery campaign for task {task_id}")
        return self._campaign_view(dict(row))

    def record_unit(
        self,
        task_id: str,
        *,
        unit_key: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Upsert a delivery unit for one worktree/branch/PR."""
        cleaned = self._clean_unit_fields(fields)
        effective_unit_key = unit_key or self._derive_unit_key(cleaned)
        values: dict[str, Any] = {
            "id": str(uuid4()),
            "task_id": task_id,
            "unit_key": effective_unit_key,
            "updated_at": _now(),
            **cleaned,
        }
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"id", "task_id", "unit_key"}
        )
        with self.db.transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO task_delivery_units ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(task_id, unit_key) DO UPDATE SET {updates}
                """,  # nosec B608 - columns are filtered against static allowlists.
                tuple(values[column] for column in columns),
            )
        return self._unit_view({"task_id": task_id, "unit_key": effective_unit_key, **cleaned})

    def get_state(self, task_id: str) -> dict[str, Any]:
        """Return the campaign row plus all unit rows for a task."""
        with self.db.transaction() as conn:
            campaign_row = conn.execute(
                """
                SELECT task_id, state, merge_strategy, structured_pr_verdict,
                       pr_report_ref, merge_sha, merge_report_ref, last_error,
                       created_at, updated_at
                  FROM task_delivery_campaigns
                 WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            unit_rows = conn.execute(
                """
                SELECT id, task_id, unit_key, worktree_id, repo, source_branch,
                       target_branch, pr_required, protection_json, pr_url,
                       github_pr_number, gate_snapshot_json, pr_state,
                       local_update_attempts, last_error, created_at, updated_at
                  FROM task_delivery_units
                 WHERE task_id = ?
                 ORDER BY created_at, unit_key
                """,
                (task_id,),
            ).fetchall()
        return {
            "task_id": task_id,
            "campaign": self._campaign_view(dict(campaign_row)) if campaign_row else None,
            "units": [self._unit_view(dict(row)) for row in unit_rows],
        }

    def _clean_campaign_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for column, value in fields.items():
            if column not in CAMPAIGN_COLUMNS:
                continue
            cleaned[column] = (
                _encode_json(value)
                if column in CAMPAIGN_JSON_COLUMNS and value is not None
                else value
            )
        return cleaned

    def _clean_unit_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for column, value in fields.items():
            if column not in UNIT_COLUMNS or value is None:
                continue
            if column == "pr_required":
                cleaned[column] = 1 if bool(value) else 0
            elif column in UNIT_JSON_COLUMNS:
                cleaned[column] = _encode_json(value)
            else:
                cleaned[column] = value
        return cleaned

    def _derive_unit_key(self, fields: dict[str, Any]) -> str:
        if fields.get("worktree_id"):
            return f"worktree:{fields['worktree_id']}"
        if fields.get("pr_url"):
            return f"pr:{fields['pr_url']}"
        source_branch = fields.get("source_branch")
        target_branch = fields.get("target_branch")
        if source_branch and target_branch:
            return f"branch:{source_branch}->{target_branch}"
        if target_branch:
            return f"target:{target_branch}"
        return "default"

    def _campaign_view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        view["structured_pr_verdict"] = _decode_json(view.get("structured_pr_verdict"))
        return view

    def _unit_view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        view["pr_required"] = _as_bool(view.get("pr_required"))
        view["protection"] = _decode_json(view.pop("protection_json", None))
        view["gate_snapshot"] = _decode_json(view.pop("gate_snapshot_json", None))
        return view
