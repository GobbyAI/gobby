"""Token-guarded terminal transitions for shadow-judge claims."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class RecallShadowClaimTransitionMixin:
    """State transitions used after a shadow-judge call finishes."""

    db: HubDatabase

    def mark_shadow_claim_retryable(
        self,
        recall_request_id: str,
        *,
        label_source: str,
        judge_protocol_version: str,
        claim_token: str,
        error: str,
        now: datetime | None = None,
    ) -> bool:
        """Release a live claim into capped exponential backoff."""
        current_time = now or utc_now()
        cursor = self.db.execute(
            """
            UPDATE recall_shadow_judge_state
            SET status = 'retryable',
                next_attempt_at = %s
                    + (LEAST(POWER(2, attempts), 24) * INTERVAL '1 hour'),
                lease_expires_at = NULL,
                claim_token = NULL,
                last_error = %s,
                updated_at = %s
            WHERE recall_request_id = %s
              AND label_source = %s
              AND judge_protocol_version = %s
              AND status = 'claimed'
              AND claim_token = %s
            """,
            (
                current_time,
                error,
                current_time,
                recall_request_id,
                label_source,
                judge_protocol_version,
                claim_token,
            ),
        )
        return cursor.rowcount == 1

    def mark_shadow_claim_terminal(
        self,
        recall_request_id: str,
        *,
        label_source: str,
        judge_protocol_version: str,
        claim_token: str,
        error: str,
        now: datetime | None = None,
    ) -> bool:
        """Stop polling a claimed request after content drift or deletion."""
        if error not in {"content_drift", "memory_deleted"}:
            raise ValueError("terminal shadow error must be content_drift or memory_deleted")
        current_time = now or utc_now()
        cursor = self.db.execute(
            """
            UPDATE recall_shadow_judge_state
            SET status = 'terminal',
                next_attempt_at = NULL,
                lease_expires_at = NULL,
                claim_token = NULL,
                last_error = %s,
                updated_at = %s
            WHERE recall_request_id = %s
              AND label_source = %s
              AND judge_protocol_version = %s
              AND status = 'claimed'
              AND claim_token = %s
            """,
            (
                error,
                current_time,
                recall_request_id,
                label_source,
                judge_protocol_version,
                claim_token,
            ),
        )
        return cursor.rowcount == 1
