"""Hub receipts for the nightly ``vector reconcile`` run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.code_index.nightly_reconcile import NightlyReconcileReceipt


class CodeIndexReconcileReceiptStorageMixin:
    """Writes one reconcile receipt row per project per nightly run."""

    db: HubDatabase

    def record_nightly_reconcile_receipt(self, receipt: NightlyReconcileReceipt) -> None:
        """Append the receipt for one project's reconcile run."""
        machine_id = require_machine_id()

        def _write(conn: Transaction) -> None:
            conn.execute(
                """INSERT INTO code_index_reconcile_receipts (
                    machine_id, run_id, project_id, mode, digest, class_counts,
                    outcome, retries, error, started_at, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)
                """,
                (
                    machine_id,
                    receipt.run_id,
                    receipt.project_id,
                    receipt.mode,
                    receipt.digest,
                    Jsonb(receipt.class_counts),
                    receipt.outcome,
                    receipt.retries,
                    receipt.error,
                    receipt.started_at,
                    receipt.finished_at,
                ),
            )

        with self.db.transaction() as conn:
            _write(conn)
