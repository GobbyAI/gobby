"""Machine-owned project checkout persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn

from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
from gobby.utils.datetime import normalize_datetime_model


class CheckoutConflictError(Exception):
    """Raised when a machine already has a checkout for the project at another root."""


class CheckoutRootTakenError(Exception):
    """Raised when another project on the machine already owns the root."""


class OverlayRegistrationRejectedError(Exception):
    """Raised when the root is a registered worktree or clone on the machine."""


class CheckoutSentinelRejectedError(Exception):
    """Raised when a checkout-free sentinel project id is used."""


@normalize_datetime_model(required=("created_at", "updated_at"))
@dataclass(frozen=True)
class ProjectCheckout:
    """One machine-owned primary checkout row."""

    machine_id: str
    project_id: str
    root_path: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ProjectCheckout:
        """Build a checkout from a database row."""
        return cls(
            machine_id=str(row["machine_id"]),
            project_id=str(row["project_id"]),
            root_path=str(row["root_path"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class LocalProjectCheckoutManager:
    """Filesystem-free CRUD for `project_checkouts`."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def get(self, machine_id: str, project_id: str) -> ProjectCheckout | None:
        """Return the checkout for `(machine_id, project_id)` if it exists."""
        row = self.db.fetchone(
            """
            SELECT machine_id, project_id, root_path, created_at, updated_at
            FROM project_checkouts
            WHERE machine_id = %s AND project_id = %s
            """,
            (machine_id, project_id),
        )
        return ProjectCheckout.from_row(row) if row is not None else None

    def list_for_machine(self, machine_id: str) -> list[ProjectCheckout]:
        """List every checkout owned by `machine_id`."""
        rows = self.db.fetchall(
            """
            SELECT machine_id, project_id, root_path, created_at, updated_at
            FROM project_checkouts
            WHERE machine_id = %s
            ORDER BY root_path
            """,
            (machine_id,),
        )
        return [ProjectCheckout.from_row(row) for row in rows]

    def register(
        self, machine_id: str, project_id: str, root_path: str
    ) -> tuple[ProjectCheckout, bool]:
        """Insert or return the same-root checkout. Different roots conflict."""
        self._reject_sentinel(project_id)
        try:
            with self.db.transaction() as conn:
                self._reject_overlay(conn, machine_id, root_path)
                inserted = conn.execute(
                    """
                    INSERT INTO project_checkouts (machine_id, project_id, root_path)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (machine_id, project_id) DO NOTHING
                    RETURNING machine_id, project_id, root_path, created_at, updated_at
                    """,
                    (machine_id, project_id, root_path),
                ).fetchone()
                if inserted is not None:
                    return ProjectCheckout.from_row(inserted), True
                existing = self._lock_checkout(conn, machine_id, project_id)
                if existing.root_path == root_path:
                    return existing, False
                raise CheckoutConflictError(
                    f"checkout for machine {machine_id} project {project_id} "
                    f"is already {existing.root_path}"
                )
        except UniqueViolation as exc:
            self._raise_root_taken(exc, machine_id, root_path)

    def rebind(self, machine_id: str, project_id: str, root_path: str) -> ProjectCheckout:
        """Insert when absent, no-op the same root, or move to a different root."""
        self._reject_sentinel(project_id)
        try:
            with self.db.transaction() as conn:
                self._reject_overlay(conn, machine_id, root_path)
                inserted = conn.execute(
                    """
                    INSERT INTO project_checkouts (machine_id, project_id, root_path)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (machine_id, project_id) DO NOTHING
                    RETURNING machine_id, project_id, root_path, created_at, updated_at
                    """,
                    (machine_id, project_id, root_path),
                ).fetchone()
                if inserted is not None:
                    return ProjectCheckout.from_row(inserted)
                existing = self._lock_checkout(conn, machine_id, project_id)
                if existing.root_path == root_path:
                    return existing
                updated = conn.execute(
                    """
                    UPDATE project_checkouts
                    SET root_path = %s, updated_at = now()
                    WHERE machine_id = %s AND project_id = %s
                    RETURNING machine_id, project_id, root_path, created_at, updated_at
                    """,
                    (root_path, machine_id, project_id),
                ).fetchone()
                if updated is None:
                    raise RuntimeError(
                        f"lost checkout row for machine {machine_id} project {project_id}"
                    )
                return ProjectCheckout.from_row(updated)
        except UniqueViolation as exc:
            self._raise_root_taken(exc, machine_id, root_path)

    @staticmethod
    def _reject_sentinel(project_id: str) -> None:
        if project_id in CHECKOUT_FREE_PROJECT_IDS:
            raise CheckoutSentinelRejectedError(
                f"checkout-free sentinel project {project_id} cannot own a checkout"
            )

    @staticmethod
    def _reject_overlay(conn: Transaction, machine_id: str, root_path: str) -> None:
        row = conn.execute(
            """
            SELECT 1 FROM worktrees
            WHERE machine_id = %s AND worktree_path = %s
            UNION ALL
            SELECT 1 FROM clones
            WHERE machine_id = %s AND clone_path = %s
            LIMIT 1
            """,
            (machine_id, root_path, machine_id, root_path),
        ).fetchone()
        if row is not None:
            raise OverlayRegistrationRejectedError(
                f"root {root_path} is a registered overlay on machine {machine_id}"
            )

    @staticmethod
    def _lock_checkout(conn: Transaction, machine_id: str, project_id: str) -> ProjectCheckout:
        row = conn.execute(
            """
            SELECT machine_id, project_id, root_path, created_at, updated_at
            FROM project_checkouts
            WHERE machine_id = %s AND project_id = %s
            FOR UPDATE
            """,
            (machine_id, project_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"checkout disappeared for machine {machine_id} project {project_id}"
            )
        return ProjectCheckout.from_row(row)

    @staticmethod
    def _raise_root_taken(exc: UniqueViolation, machine_id: str, root_path: str) -> NoReturn:
        raise CheckoutRootTakenError(
            f"root {root_path} is already owned on machine {machine_id}"
        ) from exc
