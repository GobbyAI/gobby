"""Database-enforced maintenance epochs and destructive-operation ledgers."""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gobby.utils.sql import render_internal_sql

type Campaign = Literal[
    "schema-apply",
    "purge",
    "reconcile",
]
type BatchStatus = Literal["pending", "applied", "verified", "aborted"]
type ReceiptState = Literal["pending", "applied", "verified"]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

CAMPAIGNS: tuple[Campaign, ...] = (
    "schema-apply",
    "purge",
    "reconcile",
)
MAINTENANCE_EPOCH_ENV = "GOBBY_MAINTENANCE_EPOCH"
_QUIESCENCE_DEADLINE_SECONDS = 15.0
_QUIESCENCE_POLL_SECONDS = 0.25
_PGOPTIONS_EPOCH_PATTERN = re.compile(r"(?:^|\s)-c\s+gobby\.maintenance_epoch=\S+")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MaintenanceEpoch:
    """One hub-wide maintenance fence."""

    id: uuid.UUID
    campaign: Campaign
    opened_at: datetime
    opened_by: str
    scope_note: str
    released_at: datetime | None
    released_by_command: str | None


@dataclass(frozen=True)
class DestructiveBatch:
    """Hub-resident campaign intent and component receipts."""

    id: uuid.UUID
    maintenance_epoch_id: uuid.UUID
    campaign: Campaign
    status: BatchStatus
    backup_manifest_path: str | None
    backup_manifest_sha256: str | None
    intent: JsonObject
    migration_plan: list[dict[str, str]]
    target_receipts: dict[str, dict[str, str]]
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None
    aborted_at: datetime | None
    abort_disposition: str | None


class MaintenanceEpochError(RuntimeError):
    """Base error for maintenance-epoch protocol failures."""


class MaintenanceEpochActiveError(MaintenanceEpochError):
    """Raised when a tokenless or incorrectly tokened login is fenced."""

    def __init__(self, epoch_id: uuid.UUID) -> None:
        self.epoch_id = epoch_id
        super().__init__(
            "Hub maintenance is active; "
            "use `gobby hub-maintenance status` or `gobby hub-maintenance resume`."
        )


class MaintenanceEpochOwnershipError(MaintenanceEpochError):
    """Raised when a non-owning command attempts to release an epoch."""


class MaintenanceQuiescenceError(MaintenanceEpochError):
    """Raised when pre-fence client connections survive termination."""


class MaintenanceBatchStateError(MaintenanceEpochError):
    """Raised for invalid destructive-batch transitions."""


class MaintenanceReceiptVerificationError(MaintenanceEpochError):
    """Raised when a component mutation does not reach its postcondition."""


def bind_maintenance_epoch(database_url: str, epoch_id: uuid.UUID | str) -> str:
    """Return a DSN carrying the epoch token as a startup GUC."""
    normalized_epoch = _normalize_uuid(epoch_id)
    fields = conninfo_to_dict(database_url)
    raw_options = fields.get("options")
    existing_options = str(raw_options) if raw_options is not None else ""
    options = _PGOPTIONS_EPOCH_PATTERN.sub("", existing_options).strip()
    epoch_option = f"-c gobby.maintenance_epoch={normalized_epoch}"
    fields["options"] = f"{options} {epoch_option}".strip()
    string_fields = {key: str(value) for key, value in fields.items() if value is not None}
    return make_conninfo("", **string_fields)


def _bind_login_fence_bypass(database_url: str) -> str:
    """Return a DSN using PostgreSQL's superuser-only event-trigger bypass."""
    fields = conninfo_to_dict(database_url)
    raw_options = fields.get("options")
    existing_options = str(raw_options) if raw_options is not None else ""
    fields["options"] = f"{existing_options} -c event_triggers=off".strip()
    string_fields = {key: str(value) for key, value in fields.items() if value is not None}
    return make_conninfo("", **string_fields)


def maintenance_child_environment(
    epoch_id: uuid.UUID | str,
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child-only environment carrying the maintenance token."""
    normalized_epoch = _normalize_uuid(epoch_id)
    environment = dict(source if source is not None else os.environ)
    existing_options = _PGOPTIONS_EPOCH_PATTERN.sub(
        "",
        environment.get("PGOPTIONS", ""),
    ).strip()
    epoch_option = f"-c gobby.maintenance_epoch={normalized_epoch}"
    environment["PGOPTIONS"] = f"{existing_options} {epoch_option}".strip()
    environment[MAINTENANCE_EPOCH_ENV] = str(normalized_epoch)
    return environment


def probe_maintenance_admission(database_url: str) -> str:
    """Perform the courtesy login probe and translate the database fence."""
    try:
        with _connect(
            database_url,
            autocommit=True,
            application_name="gobby-maintenance-admission",
        ):
            return database_url
    except psycopg.Error as exc:
        try:
            active_epoch = _read_active_epoch(_bind_login_fence_bypass(database_url))
        except psycopg.Error:
            raise exc from None
        if active_epoch is None:
            raise
        raise MaintenanceEpochActiveError(active_epoch.id) from exc


def admitted_database_url(database_url: str) -> str:
    """Bind an orchestrator child token, then perform the courtesy probe."""
    child_epoch = os.environ.get(MAINTENANCE_EPOCH_ENV)
    admitted_url = (
        bind_maintenance_epoch(database_url, child_epoch) if child_epoch else database_url
    )
    return probe_maintenance_admission(admitted_url)


def open_maintenance_epoch(
    database_url: str,
    *,
    campaign: Campaign,
    opened_by: str,
    scope_note: str,
    quiescence_deadline_seconds: float = _QUIESCENCE_DEADLINE_SECONDS,
    quiescence_poll_seconds: float = _QUIESCENCE_POLL_SECONDS,
) -> MaintenanceEpoch:
    """Commit the fence, terminate older Gobby clients, then prove quiescence.

    ``pg_terminate_backend`` is asynchronous, so terminated backends may
    linger briefly in ``pg_stat_activity``. Quiescence is therefore polled
    up to ``quiescence_deadline_seconds``. Every failure after the fence commit
    releases it before propagating so an unsuccessful open cannot strand clients
    behind an ownerless epoch.
    """
    _validate_campaign(campaign)
    if not opened_by.strip():
        raise ValueError("opened_by is required")
    if not scope_note.strip():
        raise ValueError("scope_note is required")

    epoch_id = _new_epoch_id()
    with _connect(
        database_url,
        autocommit=False,
        application_name=f"gobby-hub-maintenance-{campaign}",
    ) as connection:
        try:
            row = connection.execute(
                """
                INSERT INTO maintenance_epochs(
                    id,
                    campaign,
                    opened_by,
                    scope_note
                )
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (epoch_id, campaign, opened_by, scope_note),
            ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise MaintenanceEpochError(
                "A maintenance epoch is already open; use `gobby hub-maintenance status`."
            ) from exc
        if row is None:
            raise MaintenanceEpochError("The maintenance epoch insert returned no row")

        connection.commit()
        try:
            deadline = time.monotonic() + quiescence_deadline_seconds
            while True:
                connection.execute(
                    """
                    SELECT pg_catalog.pg_terminate_backend(activity.pid)
                    FROM pg_catalog.pg_stat_activity AS activity
                    WHERE activity.datname = current_database()
                      AND activity.pid <> pg_backend_pid()
                      AND activity.backend_type = 'client backend'
                      AND activity.application_name LIKE 'gobby%%'
                    """
                ).fetchall()
                # PostgreSQL caches activity statistics until the transaction snapshot is cleared.
                connection.execute("SELECT pg_catalog.pg_stat_clear_snapshot()")
                remaining = connection.execute(
                    """
                    SELECT COUNT(*) AS foreign_connections
                    FROM pg_catalog.pg_stat_activity AS activity
                    WHERE activity.datname = current_database()
                      AND activity.pid <> pg_backend_pid()
                      AND activity.backend_type = 'client backend'
                      AND activity.application_name LIKE 'gobby%%'
                    """
                ).fetchone()
                foreign_connections = (
                    int(remaining["foreign_connections"]) if remaining is not None else 0
                )
                if not foreign_connections:
                    return _epoch_from_row(row)
                if time.monotonic() >= deadline:
                    break
                time.sleep(quiescence_poll_seconds)
            raise MaintenanceQuiescenceError(
                f"Maintenance epoch {epoch_id} could not quiesce: "
                f"{foreign_connections} Gobby client connection(s) remained after "
                f"{quiescence_deadline_seconds:.1f}s; the fence was released."
            )
        except BaseException:
            try:
                _release_fence_after_failed_open(connection, epoch_id)
            except psycopg.Error as release_error:
                raise MaintenanceQuiescenceError(
                    f"Maintenance epoch {epoch_id} failed to open and the fence "
                    "could not be released; run `gobby hub-maintenance abort`."
                ) from release_error
            raise


def _release_fence_after_failed_open(
    connection: psycopg.Connection[dict[str, Any]],
    epoch_id: uuid.UUID,
) -> None:
    """Roll the fence back on the opening connection after any failed open."""
    connection.rollback()
    connection.execute(
        """
        UPDATE maintenance_epochs
        SET released_at = NOW(),
            released_by_command = 'open-maintenance-epoch failure'
        WHERE id = %s
          AND released_at IS NULL
        """,
        (epoch_id,),
    )
    connection.commit()


def discover_active_maintenance_epoch(database_url: str) -> MaintenanceEpoch | None:
    """Discover the open epoch through PostgreSQL's privileged repair path."""
    try:
        return _read_active_epoch(database_url)
    except psycopg.Error as exc:
        try:
            return _read_active_epoch(_bind_login_fence_bypass(database_url))
        except psycopg.Error:
            raise exc from None


def require_orchestrator_epoch(
    database_url: str,
    epoch_id: uuid.UUID | str,
    *,
    campaign: Campaign | None = None,
) -> MaintenanceEpoch:
    """Require an open epoch owned by the maintenance orchestrator."""
    normalized_epoch = _normalize_uuid(epoch_id)
    with _connect(
        bind_maintenance_epoch(database_url, normalized_epoch),
        autocommit=True,
        application_name="gobby-maintenance-require",
    ) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM maintenance_epochs
            WHERE id = %s
              AND released_at IS NULL
            """,
            (normalized_epoch,),
        ).fetchone()
    if row is None:
        raise MaintenanceEpochError(f"Maintenance epoch {normalized_epoch} is not open")
    epoch = _epoch_from_row(row)
    if not epoch.opened_by.startswith("hub-maintenance:"):
        raise MaintenanceEpochOwnershipError(
            f"Maintenance epoch {normalized_epoch} is not orchestrator-owned"
        )
    if campaign is not None and epoch.campaign != campaign:
        raise MaintenanceEpochOwnershipError(
            f"Maintenance epoch {normalized_epoch} owns {epoch.campaign}, not {campaign}"
        )
    return epoch


def create_destructive_batch(
    database_url: str,
    epoch_id: uuid.UUID | str,
    *,
    campaign: Campaign,
    intent: JsonObject,
    migration_plan: list[dict[str, str]] | None = None,
) -> DestructiveBatch:
    """Persist campaign intent before backup or mutation begins."""
    epoch = require_orchestrator_epoch(database_url, epoch_id, campaign=campaign)
    batch_id = uuid.uuid4()
    with _connect(
        bind_maintenance_epoch(database_url, epoch.id),
        autocommit=False,
        application_name="gobby-maintenance-batch-create",
    ) as connection:
        row = connection.execute(
            """
            INSERT INTO destructive_batches(
                id,
                maintenance_epoch_id,
                campaign,
                intent,
                migration_plan
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                batch_id,
                epoch.id,
                campaign,
                Jsonb(intent),
                Jsonb(migration_plan or []),
            ),
        ).fetchone()
        connection.commit()
    if row is None:
        raise MaintenanceEpochError("The destructive batch insert returned no row")
    return _batch_from_row(row)


def get_destructive_batch(
    database_url: str,
    epoch_id: uuid.UUID | str,
    batch_id: uuid.UUID | str | None = None,
) -> DestructiveBatch | None:
    """Read an epoch's batch from hub state."""
    normalized_epoch = _normalize_uuid(epoch_id)
    parameters: tuple[object, ...] = (normalized_epoch,)
    predicate = "maintenance_epoch_id = %s"
    if batch_id is not None:
        predicate += " AND id = %s"
        parameters += (_normalize_uuid(batch_id),)
    with _connect(
        bind_maintenance_epoch(database_url, normalized_epoch),
        autocommit=True,
        application_name="gobby-maintenance-batch-read",
    ) as connection:
        row = connection.execute(
            render_internal_sql(
                """
                SELECT *
                FROM destructive_batches
                WHERE {predicate}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                predicate=predicate,
            ),
            parameters,
        ).fetchone()
    return _batch_from_row(row) if row is not None else None


def record_batch_backup(
    database_url: str,
    epoch_id: uuid.UUID | str,
    batch_id: uuid.UUID | str,
    *,
    manifest_path: str,
    manifest_sha256: str,
) -> DestructiveBatch:
    """Bind the fresh epoch backup to its hub-resident batch."""
    if not manifest_path:
        raise ValueError("manifest_path is required")
    if _SHA256_PATTERN.fullmatch(manifest_sha256) is None:
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    return _update_batch(
        database_url,
        epoch_id,
        batch_id,
        """
        backup_manifest_path = %s,
        backup_manifest_sha256 = %s,
        updated_at = NOW()
        """,
        (manifest_path, manifest_sha256),
        allowed_statuses=("pending",),
    )


def mark_batch_applied(
    database_url: str,
    epoch_id: uuid.UUID | str,
    batch_id: uuid.UUID | str,
) -> DestructiveBatch:
    """Record successful mutation/apply completion."""
    return _update_batch(
        database_url,
        epoch_id,
        batch_id,
        "status = 'applied', updated_at = NOW()",
        (),
        allowed_statuses=("pending",),
    )


def mark_batch_verified(
    database_url: str,
    epoch_id: uuid.UUID | str,
    batch_id: uuid.UUID | str,
) -> DestructiveBatch:
    """Record successful campaign verification."""
    return _update_batch(
        database_url,
        epoch_id,
        batch_id,
        "status = 'verified', verified_at = NOW(), updated_at = NOW()",
        (),
        allowed_statuses=("applied",),
    )


def release_maintenance_epoch(
    database_url: str,
    epoch_id: uuid.UUID | str,
    *,
    owner_command: str,
    released_by_command: str,
) -> MaintenanceEpoch:
    """Release a verified epoch only for its owning orchestrator."""
    normalized_epoch = _normalize_uuid(epoch_id)
    with _connect(
        bind_maintenance_epoch(database_url, normalized_epoch),
        autocommit=False,
        application_name="gobby-maintenance-release",
    ) as connection:
        epoch_row = connection.execute(
            """
            SELECT *
            FROM maintenance_epochs
            WHERE id = %s
              AND released_at IS NULL
            FOR UPDATE
            """,
            (normalized_epoch,),
        ).fetchone()
        if epoch_row is None:
            raise MaintenanceEpochError(f"Maintenance epoch {normalized_epoch} is not open")
        epoch = _epoch_from_row(epoch_row)
        if epoch.opened_by != owner_command:
            raise MaintenanceEpochOwnershipError(
                f"Maintenance epoch {normalized_epoch} is owned by {epoch.opened_by}"
            )
        batch = connection.execute(
            """
            SELECT status
            FROM destructive_batches
            WHERE maintenance_epoch_id = %s
            """,
            (normalized_epoch,),
        ).fetchone()
        if batch is None or batch["status"] != "verified":
            raise MaintenanceBatchStateError(
                f"Maintenance epoch {normalized_epoch} cannot release before its batch is verified"
            )
        released_row = connection.execute(
            """
            UPDATE maintenance_epochs
            SET released_at = NOW(),
                released_by_command = %s
            WHERE id = %s
            RETURNING *
            """,
            (released_by_command, normalized_epoch),
        ).fetchone()
        connection.commit()
    if released_row is None:
        raise MaintenanceEpochError(f"Maintenance epoch {normalized_epoch} was not released")
    return _epoch_from_row(released_row)


def release_restored_maintenance_epoch(database_url: str) -> MaintenanceEpoch | None:
    """Release an open epoch copied into a disaster-recovery target."""
    epoch = discover_active_maintenance_epoch(database_url)
    if epoch is None:
        return None

    with _connect(
        bind_maintenance_epoch(database_url, epoch.id),
        autocommit=False,
        application_name="gobby-hub-backup-restore",
    ) as connection:
        released_row = connection.execute(
            """
            UPDATE maintenance_epochs
            SET released_at = NOW(),
                released_by_command = 'restore'
            WHERE id = %s
              AND released_at IS NULL
            RETURNING *
            """,
            (epoch.id,),
        ).fetchone()
        connection.commit()
    return _epoch_from_row(released_row) if released_row is not None else None


def abort_maintenance_epoch(
    database_url: str,
    epoch_id: uuid.UUID | str,
    *,
    disposition: str,
    confirmed: bool,
) -> MaintenanceEpoch:
    """Record partial-state evidence and explicitly release an epoch."""
    if not confirmed:
        raise ValueError("explicit abort confirmation is required")
    if not disposition.strip():
        raise ValueError("abort disposition is required")
    normalized_epoch = _normalize_uuid(epoch_id)
    with _connect(
        bind_maintenance_epoch(database_url, normalized_epoch),
        autocommit=False,
        application_name="gobby-maintenance-abort",
    ) as connection:
        connection.execute(
            """
            UPDATE destructive_batches
            SET status = 'aborted',
                aborted_at = NOW(),
                abort_disposition = %s,
                verified_at = NULL,
                updated_at = NOW()
            WHERE maintenance_epoch_id = %s
              AND status <> 'verified'
              AND status <> 'aborted'
            """,
            (disposition.strip(), normalized_epoch),
        )
        row = connection.execute(
            """
            UPDATE maintenance_epochs
            SET released_at = NOW(),
                released_by_command = 'hub-maintenance abort'
            WHERE id = %s
              AND released_at IS NULL
            RETURNING *
            """,
            (normalized_epoch,),
        ).fetchone()
        connection.commit()
    if row is None:
        raise MaintenanceEpochError(f"Maintenance epoch {normalized_epoch} is not open")
    return _epoch_from_row(row)


def run_receipted_component(
    database_url: str,
    epoch_id: uuid.UUID | str,
    batch_id: uuid.UUID | str,
    *,
    target: str,
    apply: Callable[[], None],
    postcondition: Callable[[], bool],
) -> DestructiveBatch:
    """Run or resume one idempotent external component mutation."""
    if not target.strip():
        raise ValueError("receipt target is required")
    batch = get_destructive_batch(database_url, epoch_id, batch_id)
    if batch is None:
        raise MaintenanceEpochError(f"Destructive batch {batch_id} does not exist")
    receipt = batch.target_receipts.get(target)
    if receipt is not None and receipt.get("state") == "verified":
        return batch

    batch = _set_receipt_state(
        database_url,
        epoch_id,
        batch_id,
        target=target,
        state="pending",
    )
    if postcondition():
        batch = _set_receipt_state(
            database_url,
            epoch_id,
            batch_id,
            target=target,
            state="applied",
        )
        return _set_receipt_state(
            database_url,
            epoch_id,
            batch_id,
            target=target,
            state="verified",
        )

    apply()
    batch = _set_receipt_state(
        database_url,
        epoch_id,
        batch_id,
        target=target,
        state="applied",
    )
    if not postcondition():
        raise MaintenanceReceiptVerificationError(
            f"Component {target!r} did not reach its required postcondition"
        )
    return _set_receipt_state(
        database_url,
        epoch_id,
        batch_id,
        target=target,
        state="verified",
    )


def _read_active_epoch(database_url: str) -> MaintenanceEpoch | None:
    with _connect(
        database_url,
        autocommit=True,
        application_name="gobby-maintenance-discover",
    ) as connection:
        relation = connection.execute(
            "SELECT pg_catalog.to_regclass('maintenance_epochs') AS relation"
        ).fetchone()
        if relation is None or relation["relation"] is None:
            return None
        row = connection.execute(
            """
            SELECT *
            FROM maintenance_epochs
            WHERE released_at IS NULL
            """
        ).fetchone()
    return _epoch_from_row(row) if row is not None else None


def _update_batch(
    database_url: str,
    epoch_id: uuid.UUID | str,
    batch_id: uuid.UUID | str,
    assignments: str,
    assignment_parameters: tuple[object, ...],
    *,
    allowed_statuses: tuple[BatchStatus, ...],
) -> DestructiveBatch:
    normalized_epoch = _normalize_uuid(epoch_id)
    normalized_batch = _normalize_uuid(batch_id)
    with _connect(
        bind_maintenance_epoch(database_url, normalized_epoch),
        autocommit=False,
        application_name="gobby-maintenance-batch-update",
    ) as connection:
        row = connection.execute(
            render_internal_sql(
                """
                UPDATE destructive_batches
                SET {assignments}
                WHERE id = %s
                  AND maintenance_epoch_id = %s
                  AND status = ANY(%s)
                RETURNING *
                """,
                assignments=assignments,
            ),
            (
                *assignment_parameters,
                normalized_batch,
                normalized_epoch,
                list(allowed_statuses),
            ),
        ).fetchone()
        connection.commit()
    if row is None:
        raise MaintenanceBatchStateError(
            f"Destructive batch {normalized_batch} is absent or cannot transition "
            f"from statuses {allowed_statuses}"
        )
    return _batch_from_row(row)


def _set_receipt_state(
    database_url: str,
    epoch_id: uuid.UUID | str,
    batch_id: uuid.UUID | str,
    *,
    target: str,
    state: ReceiptState,
) -> DestructiveBatch:
    normalized_epoch = _normalize_uuid(epoch_id)
    normalized_batch = _normalize_uuid(batch_id)
    receipt = {"state": state, "updated_at": datetime.now(UTC).isoformat()}
    with _connect(
        bind_maintenance_epoch(database_url, normalized_epoch),
        autocommit=False,
        application_name="gobby-maintenance-receipt",
    ) as connection:
        row = connection.execute(
            """
            UPDATE destructive_batches
            SET target_receipts = jsonb_set(
                    target_receipts,
                    ARRAY[%s]::TEXT[],
                    %s,
                    TRUE
                ),
                updated_at = NOW()
            WHERE id = %s
              AND maintenance_epoch_id = %s
              AND status IN ('pending', 'applied')
            RETURNING *
            """,
            (target, Jsonb(receipt), normalized_batch, normalized_epoch),
        ).fetchone()
        connection.commit()
    if row is None:
        raise MaintenanceBatchStateError(
            f"Destructive batch {normalized_batch} cannot record target {target!r}"
        )
    return _batch_from_row(row)


def _connect(
    database_url: str,
    *,
    autocommit: bool,
    application_name: str,
) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        database_url,
        autocommit=autocommit,
        application_name=application_name,
        connect_timeout=5,
        row_factory=dict_row,
    )


def _normalize_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


def _new_epoch_id() -> uuid.UUID:
    return uuid.uuid4()


def _validate_campaign(campaign: str) -> None:
    if campaign not in CAMPAIGNS:
        raise ValueError(f"Unknown maintenance campaign: {campaign}")


def _epoch_from_row(row: Mapping[str, Any]) -> MaintenanceEpoch:
    return MaintenanceEpoch(
        id=cast(uuid.UUID, row["id"]),
        campaign=cast(Campaign, row["campaign"]),
        opened_at=cast(datetime, row["opened_at"]),
        opened_by=str(row["opened_by"]),
        scope_note=str(row["scope_note"]),
        released_at=cast(datetime | None, row["released_at"]),
        released_by_command=cast(str | None, row["released_by_command"]),
    )


def _batch_from_row(row: Mapping[str, Any]) -> DestructiveBatch:
    return DestructiveBatch(
        id=cast(uuid.UUID, row["id"]),
        maintenance_epoch_id=cast(uuid.UUID, row["maintenance_epoch_id"]),
        campaign=cast(Campaign, row["campaign"]),
        status=cast(BatchStatus, row["status"]),
        backup_manifest_path=cast(str | None, row["backup_manifest_path"]),
        backup_manifest_sha256=cast(str | None, row["backup_manifest_sha256"]),
        intent=cast(JsonObject, row["intent"]),
        migration_plan=cast(list[dict[str, str]], row["migration_plan"]),
        target_receipts=cast(dict[str, dict[str, str]], row["target_receipts"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
        verified_at=cast(datetime | None, row["verified_at"]),
        aborted_at=cast(datetime | None, row["aborted_at"]),
        abort_disposition=cast(str | None, row["abort_disposition"]),
    )
