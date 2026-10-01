"""Delivery ledger for Telegram decision answers owed to comms sessions.

A responder-delivered answer carries its delivery state in its own
``comms_messages`` metadata, and every transition is a compare-and-set on
``(answer_outcome, answer_attempt)``:

- ``pending`` becomes ``started`` when a responder turn claims it, stamped with the
  claiming daemon's epoch, or ``blocked`` when current delivery policy refuses it.
- ``started`` becomes ``delivered``, ``failed`` or ``in_doubt``. A later daemon
  sweeps a ``started`` answer claimed by another epoch to ``in_doubt``.
- ``failed`` and ``in_doubt`` return to ``pending`` at the next attempt only when a
  person clicks Retry answer.

A claimed turn is never rerun without that click, because it may already have acted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from gobby.communications.models import AnswerOutcome, CommsMessage
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase, Transaction

# Joins an answer to its asking session so every listing stays on this machine.
_ON_THIS_MACHINE = """
    FROM comms_messages AS answer
    JOIN inter_session_messages AS delivery ON delivery.id = answer.id
    JOIN sessions AS asker ON asker.id = delivery.to_session
   WHERE asker.machine_id = %s
     AND answer.metadata_json->>'answer_delivery' = 'responder'
"""


class DecisionAnswerStore:
    """Compare-and-set transitions for responder-delivered decision answers."""

    def __init__(self, db: HubDatabase, *, machine_id: str | None = None) -> None:
        self.db = db
        self.machine_id = machine_id or require_machine_id()

    def get_answer(self, answer_id: str) -> CommsMessage | None:
        row = self.db.fetchone("SELECT * FROM comms_messages WHERE id = %s", (answer_id,))
        return CommsMessage.from_row(dict(row)) if row else None

    def claim_answer(self, answer_id: str, attempt: int, epoch: str) -> bool:
        """Start ``attempt``; exactly one caller wins and the turn is never offered again."""
        with self.db.transaction() as conn:
            claimed = _transition(
                conn,
                answer_id,
                attempt,
                ("pending",),
                {"answer_outcome": "started", "answer_epoch": epoch},
            )
            if claimed is not None:
                _close_mailbox_row(conn, answer_id)
        return claimed is not None

    def block_answer(self, answer_id: str, attempt: int) -> CommsMessage | None:
        """Refuse a pending answer that current delivery policy no longer allows."""
        with self.db.transaction() as conn:
            blocked = _transition(
                conn, answer_id, attempt, ("pending",), {"answer_outcome": "blocked"}
            )
            if blocked is not None:
                _close_mailbox_row(conn, answer_id)
        return blocked

    def settle_answer(
        self, answer_id: str, attempt: int, outcome: AnswerOutcome
    ) -> CommsMessage | None:
        """End a started attempt as ``delivered``, ``failed`` or ``in_doubt``."""
        with self.db.transaction() as conn:
            return _transition(conn, answer_id, attempt, ("started",), {"answer_outcome": outcome})

    def consume_retry(self, answer_id: str, attempt: int, clicked_by: str) -> CommsMessage | None:
        """Turn one Retry answer click into the next pending attempt; later clicks lose."""
        with self.db.transaction() as conn:
            return _transition(
                conn,
                answer_id,
                attempt,
                ("failed", "in_doubt"),
                {
                    "answer_outcome": "pending",
                    "answer_attempt": attempt + 1,
                    "answer_retry_clicked_by": clicked_by,
                },
            )

    def sweep_in_doubt_answers(self, epoch: str) -> list[CommsMessage]:
        """Mark this machine's answers started by another daemon epoch as in doubt."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""UPDATE comms_messages AS swept
                       SET metadata_json = swept.metadata_json
                                           || '{{"answer_outcome": "in_doubt"}}'::jsonb
                     WHERE swept.id IN (
                               SELECT answer.id {_ON_THIS_MACHINE}
                                  AND answer.metadata_json->>'answer_outcome' = 'started'
                                  AND answer.metadata_json->>'answer_epoch' IS DISTINCT FROM %s)
                 RETURNING swept.*""",
                (self.machine_id, epoch),
            ).fetchall()
        return [CommsMessage.from_row(dict(row)) for row in rows]

    def list_pending_answers(self) -> list[CommsMessage]:
        """This machine's answers accepted or retried but not yet claimed by a turn."""
        return self._list("AND answer.metadata_json->>'answer_outcome' = 'pending'")

    def list_unshown_statuses(self) -> list[CommsMessage]:
        """This machine's answers whose current status has not reached the decision message."""
        return [
            answer
            for answer in self._list(
                "AND answer.metadata_json->>'answer_outcome' <> 'started'"
                " AND answer.metadata_json ? 'answer_outcome'"
            )
            if answer.answer_status_key
            not in {None, answer.metadata_json.get("answer_status_shown")}
        ]

    def mark_status_shown(self, answer_id: str, key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE comms_messages
                      SET metadata_json = jsonb_set(metadata_json, '{answer_status_shown}',
                                                    to_jsonb(%s::text))
                    WHERE id = %s""",
                (key, answer_id),
            )

    def advance_answered_generation(self, decision_id: str, generation: int) -> bool:
        """Advance an answered decision's keyboard generation if it is still ``generation``."""
        with self.db.transaction() as conn:
            row = conn.execute(
                """UPDATE comms_messages
                      SET metadata_json = jsonb_set(
                              metadata_json, '{callback_generation}', to_jsonb(%s::int + 1))
                    WHERE id = %s
                      AND metadata_json->>'callback_state' = 'answered'
                      AND COALESCE((metadata_json->>'callback_generation')::int, 0) = %s
                RETURNING id""",
                (generation, decision_id, generation),
            ).fetchone()
        return row is not None

    def _list(self, condition: str) -> list[CommsMessage]:
        rows = self.db.fetchall(
            f"SELECT answer.* {_ON_THIS_MACHINE} {condition} ORDER BY answer.created_at",
            (self.machine_id,),
        )
        return [CommsMessage.from_row(dict(row)) for row in rows]


def _transition(
    conn: Transaction,
    answer_id: str,
    attempt: int,
    from_outcomes: Sequence[str],
    changes: dict[str, Any],
) -> CommsMessage | None:
    row = conn.execute(
        """UPDATE comms_messages
              SET metadata_json = metadata_json || %s::jsonb
            WHERE id = %s
              AND metadata_json->>'answer_outcome' = ANY(%s)
              AND (metadata_json->>'answer_attempt')::int = %s
        RETURNING *""",
        (json.dumps(changes), answer_id, list(from_outcomes), attempt),
    ).fetchone()
    return CommsMessage.from_row(dict(row)) if row else None


def _close_mailbox_row(conn: Transaction, answer_id: str) -> None:
    # The responder, never a mailbox reader, consumes this row; closing it keeps it
    # out of mailbox listings once the answer's delivery has a recorded outcome.
    conn.execute(
        "UPDATE inter_session_messages SET delivered_at = %s WHERE id = %s AND delivered_at IS NULL",
        (utc_now(), answer_id),
    )
