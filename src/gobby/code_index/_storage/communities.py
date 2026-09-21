"""Community label storage helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gobby.code_index._storage.constants import SYNC_FAILURE_COOLOFF_SECONDS
from gobby.code_index.models import StoredCommunity
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id


class CodeIndexCommunityStorageMixin:
    """Storage methods for community label generation state."""

    db: HubDatabase

    def get_unlabeled_communities(
        self,
        project_id: str,
        limit: int,
        failure_cooloff_seconds: int = SYNC_FAILURE_COOLOFF_SECONDS,
    ) -> list[StoredCommunity]:
        retry_cutoff = (datetime.now(UTC) - timedelta(seconds=failure_cooloff_seconds)).isoformat()
        rows = self.db.fetchall(
            """SELECT * FROM code_communities
               WHERE machine_id = %s AND project_id = %s
                 AND labeled_signature IS DISTINCT FROM member_signature
                 AND (label_attempted_at IS NULL OR label_attempted_at < %s)
               ORDER BY member_count DESC
               LIMIT %s""",
            (require_machine_id(), project_id, retry_cutoff, limit),
        )
        return [StoredCommunity.from_row(row) for row in rows]

    def update_community_label(
        self,
        project_id: str,
        community_id: int,
        member_signature: str,
        *,
        label: str,
        label_source: str,
        label_confidence: float | None,
        label_model: str | None,
        labeled_signature: str,
    ) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_communities
                   SET label = %s,
                       label_source = %s,
                       label_confidence = %s,
                       label_model = %s,
                       labeled_signature = %s,
                       labeled_at = NOW(),
                       label_attempted_at = NULL
                   WHERE machine_id = %s
                     AND project_id = %s
                     AND community_id = %s
                     AND member_signature = %s""",
                (
                    label,
                    label_source,
                    label_confidence,
                    label_model,
                    labeled_signature,
                    require_machine_id(),
                    project_id,
                    community_id,
                    member_signature,
                ),
            )
            return cursor.rowcount > 0

    def mark_community_labels_attempted(
        self,
        rows: list[tuple[str, int, str]],
    ) -> int:
        if not rows:
            return 0
        now = datetime.now(UTC).isoformat()
        placeholders = ",".join("(%s::uuid, %s, %s)" for _ in rows)
        params = [value for row in rows for value in row]
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"""UPDATE code_communities AS c
                    SET label_attempted_at = %s
                    FROM (VALUES {placeholders}) AS v(
                        project_id, community_id, member_signature
                    )
                    WHERE c.machine_id = %s
                      AND c.project_id = v.project_id
                      AND c.community_id = v.community_id
                      AND c.member_signature = v.member_signature
                      AND c.labeled_signature IS DISTINCT FROM c.member_signature""",
                (now, *params, require_machine_id()),
            )
            return cursor.rowcount
