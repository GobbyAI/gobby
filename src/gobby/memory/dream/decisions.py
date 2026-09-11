"""Durable Dream decisions, separate from the authoritative reversal snapshots."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from gobby.memory.dream.models import DreamAction, DreamCandidate
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.json_helpers import json_dumps


class DreamDecisionStore:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def stage(
        self, run_id: str, actions: list[DreamAction], candidates: list[DreamCandidate]
    ) -> list[str]:
        """Record the complete proposed unit before any action can be applied."""
        selected = {candidate.id: candidate for candidate in candidates}
        ids: list[str] = []
        with self.db.transaction() as conn:
            conn.execute("SELECT id FROM memory_dream_runs WHERE id = %s FOR UPDATE", (run_id,))
            row = conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) AS n FROM memory_dream_decisions WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            assert row is not None
            ordinal = int(row["n"])
            for index, action in enumerate(actions, 1):
                decision_id = str(uuid4())
                candidate = selected.get(action.memory_id or "")
                effective = action.to_dict()
                if action.action == "promote" and candidate is not None and candidate.is_global:
                    effective = {**effective, "action": "keep", "normalization": "already global"}
                conn.execute(
                    """INSERT INTO memory_dream_decisions
                    (id, run_id, ordinal, proposals, effective_action, candidate, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
                    (
                        decision_id,
                        run_id,
                        ordinal + index,
                        json_dumps(
                            [{**proposal, "unit": ordinal} for proposal in action.proposals]
                        ),
                        json_dumps(effective),
                        json_dumps(
                            {
                                **candidate.to_prompt_dict(),
                                "dream_due_version": candidate.dream_due_version,
                            }
                        )
                        if candidate is not None
                        else None,
                    ),
                )
                ids.append(decision_id)
        return ids

    def finish(self, decision_id: str, status: str, **outcome: Any) -> None:
        """Add diagnostics without relabeling an already committed source mutation."""
        self.db.execute(
            """UPDATE memory_dream_decisions SET
            status = CASE WHEN status IN ('applied', 'noop', 'skipped') THEN status ELSE %s END,
            outcome = outcome || %s::jsonb, completed_at = now() WHERE id = %s""",
            (status, json_dumps(outcome), decision_id),
        )

    def get(self, decision_id: str) -> dict[str, Any]:
        row = self.db.fetchone(
            "SELECT status, outcome FROM memory_dream_decisions WHERE id = %s", (decision_id,)
        )
        if row is None:
            raise ValueError(f"Unknown Dream decision: {decision_id}")
        result = dict(row)
        if isinstance(result["outcome"], str):
            result["outcome"] = json.loads(result["outcome"])
        return result

    def page(self, run_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative and limit must be between 1 and 100")
        run = self.db.fetchone("SELECT id FROM memory_dream_runs WHERE id = %s", (run_id,))
        if run is None:
            raise ValueError(f"Unknown Dream run: {run_id}")
        rows = self.db.fetchall(
            "SELECT * FROM memory_dream_decisions WHERE run_id = %s ORDER BY ordinal LIMIT %s OFFSET %s",
            (run_id, limit + 1, offset),
        )
        decisions = []
        for row in rows[:limit]:
            item = dict(row)
            for key in ("proposals", "effective_action", "candidate", "outcome"):
                if isinstance(item[key], str):
                    item[key] = json.loads(item[key])
            decisions.append(item)
        evidence = {
            "run_id": run_id,
            "decisions": decisions,
            "next_offset": offset + limit if len(rows) > limit else None,
            "historical_rationale_missing": not rows
            and self.db.fetchone(
                "SELECT id FROM memory_dream_decisions WHERE run_id = %s LIMIT 1", (run_id,)
            )
            is None,
        }
        if evidence["historical_rationale_missing"]:
            snapshots = self.db.fetchall(
                "SELECT * FROM memory_dream_snapshots WHERE run_id = %s ORDER BY id LIMIT %s OFFSET %s",
                (run_id, limit + 1, offset),
            )
            evidence["snapshots"] = [dict(row) for row in snapshots[:limit]]
            evidence["next_offset"] = offset + limit if len(snapshots) > limit else None
        return evidence

    def interrupt_pending(self, run_id: str) -> None:
        self.db.execute(
            """UPDATE memory_dream_decisions SET status = 'interrupted', completed_at = now(),
            outcome = outcome || '{"error":"source run interrupted before a recorded outcome"}'::jsonb
            WHERE run_id = %s AND status = 'pending'""",
            (run_id,),
        )

    def summary(self, run_id: str) -> dict[str, Any]:
        rows = self.db.fetchall(
            """SELECT status, effective_action->>'action' AS action, COUNT(*) AS n,
            SUM(COALESCE((outcome->>'mutations')::int, 0)) AS mutations,
            COUNT(*) FILTER (WHERE outcome ? 'error') AS errors
            FROM memory_dream_decisions WHERE run_id = %s GROUP BY status, effective_action->>'action'""",
            (run_id,),
        )
        if not rows:
            return {}
        result: dict[str, Any] = {
            "mutations": 0,
            "noops": 0,
            "skipped": 0,
            "errors": 0,
            "decision_count": 0,
            "effective_actions": {},
        }
        for row in rows:
            count = int(row["n"])
            result["decision_count"] += count
            result["mutations"] += int(row["mutations"])
            result["errors"] += int(row["errors"])
            result["noops"] += count if row["status"] == "noop" else 0
            result["skipped"] += count if row["status"] == "skipped" else 0
            action = str(row["action"])
            result["effective_actions"][action] = result["effective_actions"].get(action, 0) + count
        proposals = self.db.fetchall(
            """SELECT COALESCE(proposal->'proposal'->>'action', 'invalid') AS action, COUNT(*) AS n FROM (
              SELECT DISTINCT proposal FROM memory_dream_decisions,
                jsonb_array_elements(proposals) AS proposal WHERE run_id = %s
            ) AS proposed GROUP BY COALESCE(proposal->'proposal'->>'action', 'invalid')""",
            (run_id,),
        )
        result["proposed_actions"] = {str(row["action"]): int(row["n"]) for row in proposals}
        return result
