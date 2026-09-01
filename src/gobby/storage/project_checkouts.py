"""Machine-owned project checkout persistence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn

from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS
from gobby.storage.workspace_machine_scope import require_local_machine_id
from gobby.utils.datetime import normalize_datetime_model


class CheckoutConflictError(Exception):
    """Raised when a machine already has a checkout for the project at another root."""


class CheckoutRootTakenError(Exception):
    """Raised when another project on the machine already owns the root."""


class OverlayRegistrationRejectedError(ValueError):
    """Raised when the root is a registered worktree or clone on the machine."""


class CheckoutSentinelRejectedError(ValueError):
    """Raised when a checkout-free sentinel project id is used."""


class SoftDeletedProjectRejectedError(ValueError):
    """Raised when hook or HTTP register targets a soft-deleted project."""


class MissingMachineContextError(ValueError):
    """Raised when a resolver is called without a machine id."""


class CheckoutNotFoundError(ValueError):
    """Raised when the primary checkout row is missing."""


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
        """Insert or return the same-root checkout. Different roots conflict.

        The insert runs under a savepoint so a root-taken violation rolls back
        to it and the surrounding (possibly ambient) transaction stays usable.
        """
        self._reject_sentinel(project_id)
        with self.db.transaction() as conn:
            self._reject_overlay(conn, machine_id, root_path)
            savepoint = conn.savepoint("project_checkout_register")
            try:
                inserted = conn.execute(
                    """
                    INSERT INTO project_checkouts (machine_id, project_id, root_path)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (machine_id, project_id) DO NOTHING
                    RETURNING machine_id, project_id, root_path, created_at, updated_at
                    """,
                    (machine_id, project_id, root_path),
                ).fetchone()
            except UniqueViolation as exc:
                savepoint.rollback()
                raced = self._fetch_checkout(conn, machine_id, project_id)
                if raced is not None and raced.root_path == root_path:
                    return raced, False
                self._raise_root_taken(exc, machine_id, root_path)
            savepoint.release()
            if inserted is not None:
                return ProjectCheckout.from_row(inserted), True
            existing = self._lock_checkout(conn, machine_id, project_id)
            if existing.root_path == root_path:
                return existing, False
            raise CheckoutConflictError(
                f"checkout for machine {machine_id} project {project_id} "
                f"is already {existing.root_path}"
            )

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
                    indexed = conn.execute(
                        """
                        SELECT root_path
                        FROM code_indexed_project_states
                        WHERE machine_id = %s AND project_id = %s
                        """,
                        (machine_id, project_id),
                    ).fetchone()
                    if indexed is not None and indexed["root_path"] != root_path:
                        self._clear_index_state(conn, machine_id, project_id)
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
                self._clear_index_state(conn, machine_id, project_id)
                return ProjectCheckout.from_row(updated)
        except UniqueViolation as exc:
            self._raise_root_taken(exc, machine_id, root_path)

    def unregister_project(self, project_id: str, *, conn: Transaction | None = None) -> int:
        """Delete the project's checkout rows on every machine; return how many went.

        Mirrors purge, whose `projects` delete cascades through
        `project_checkouts_project_id_fkey`, so a soft-deleted project frees its
        roots everywhere. Runs inside `conn` when the caller owns the transaction.
        """
        if conn is None:
            with self.db.transaction() as owned:
                return self._delete_project_checkouts(owned, project_id)
        return self._delete_project_checkouts(conn, project_id)

    @staticmethod
    def _delete_project_checkouts(conn: Transaction, project_id: str) -> int:
        cursor = conn.execute(
            "DELETE FROM project_checkouts WHERE project_id = %s",
            (project_id,),
        )
        return cursor.rowcount

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
    def _clear_index_state(conn: Transaction, machine_id: str, project_id: str) -> None:
        conn.execute(
            """
            DELETE FROM code_indexed_file_states
            WHERE machine_id = %s AND project_id = %s
            """,
            (machine_id, project_id),
        )
        conn.execute(
            """
            DELETE FROM code_indexed_project_states
            WHERE machine_id = %s AND project_id = %s
            """,
            (machine_id, project_id),
        )

    @staticmethod
    def _fetch_checkout(
        conn: Transaction, machine_id: str, project_id: str
    ) -> ProjectCheckout | None:
        row = conn.execute(
            """
            SELECT machine_id, project_id, root_path, created_at, updated_at
            FROM project_checkouts
            WHERE machine_id = %s AND project_id = %s
            """,
            (machine_id, project_id),
        ).fetchone()
        return ProjectCheckout.from_row(row) if row is not None else None

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


def _session_machine_id(project_id: str, machine_id: str | None) -> str:
    if machine_id is None or machine_id == "":
        raise MissingMachineContextError("machine_id is required to resolve a checkout")
    return require_local_machine_id(
        machine_id, resource_kind="project_checkout", resource_id=project_id
    )


def _reject_checkout_sentinel(project_id: str) -> None:
    if project_id in CHECKOUT_FREE_PROJECT_IDS:
        raise CheckoutSentinelRejectedError(
            f"checkout-free sentinel project {project_id} cannot own a checkout"
        )


def _canonical_path(path: str) -> str:
    # Same rule as gobby.utils.checkout_root.canonical_checkout_root (realpath of
    # normpath), applied locally so a symlinked caller path matches the registered root.
    return os.path.realpath(os.path.normpath(path))


def _registered_operation_overlay(
    db: HubDatabase, machine_id: str, project_id: str, overlay_path: str
) -> str | None:
    """Return the registered overlay path matching `overlay_path` canonical or raw."""
    candidates = list(dict.fromkeys((_canonical_path(overlay_path), overlay_path)))
    row = db.fetchone(
        """
        SELECT worktree_path AS path FROM worktrees
        WHERE machine_id = %s AND project_id = %s AND worktree_path = ANY(%s)
        UNION ALL
        SELECT clone_path AS path FROM clones
        WHERE machine_id = %s AND project_id = %s AND clone_path = ANY(%s)
        LIMIT 1
        """,
        (machine_id, project_id, candidates, machine_id, project_id, candidates),
    )
    return None if row is None else str(row["path"])


def require_root(db: HubDatabase, project_id: str, machine_id: str | None) -> str:
    """Return the primary checkout root for `(project_id, machine_id)`.

    Missing or empty `machine_id` is `MissingMachineContextError` with no daemon
    or logical-project fallback.
    """
    local_machine_id = _session_machine_id(project_id, machine_id)
    _reject_checkout_sentinel(project_id)
    checkout = LocalProjectCheckoutManager(db).get(local_machine_id, project_id)
    if checkout is None:
        raise CheckoutNotFoundError(
            f"no checkout for machine {local_machine_id} project {project_id}"
        )
    return checkout.root_path


def resolve_operation_root(
    db: HubDatabase,
    project_id: str,
    machine_id: str | None,
    *,
    overlay_path: str | None = None,
) -> str:
    """Return the registered overlay path when one matches, else the primary checkout.

    `overlay_path` is matched canonically (realpath of normpath) as well as raw,
    and the registered spelling is returned, so a symlinked cwd resolves to the
    overlay registered under its canonical path.
    """
    local_machine_id = _session_machine_id(project_id, machine_id)
    _reject_checkout_sentinel(project_id)
    if overlay_path is None:
        checkout = LocalProjectCheckoutManager(db).get(local_machine_id, project_id)
        if checkout is None:
            raise CheckoutNotFoundError(
                f"no checkout for machine {local_machine_id} project {project_id}"
            )
        return checkout.root_path
    registered = _registered_operation_overlay(db, local_machine_id, project_id, overlay_path)
    if registered is not None:
        return registered
    raise OverlayRegistrationRejectedError(
        f"overlay {overlay_path} is not a registered worktree or clone "
        f"for machine {local_machine_id} project {project_id}"
    )
