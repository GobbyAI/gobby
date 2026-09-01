"""Transactional storage transition for canonical account identity."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gobby.storage.maintenance_epoch import Campaign

ACCOUNT_IDENTITY_CAMPAIGN: Campaign = "account-identity-cutover"
PREDECESSOR_BASELINE_CHECKSUM = "855576453641152d2ef9199dc418fcc3dd2ad69e78eff924b05a7b3b122cf398"
BASELINE_VERSION = 375

_MUTABLE_COUNT_TABLES = frozenset(
    {
        "auth_sessions",
        "destructive_batches",
        "maintenance_epochs",
        "users",
    }
)
_OLD_CAMPAIGNS = ("schema-apply", "purge", "reconcile", "flatten")
_ACCOUNT_IDENTITY_CAMPAIGNS = (ACCOUNT_IDENTITY_CAMPAIGN, *_OLD_CAMPAIGNS)
_TARGET_CAMPAIGNS = (
    ACCOUNT_IDENTITY_CAMPAIGN,
    "project-checkout-cutover",
    "schema-apply",
    "purge",
    "reconcile",
    "flatten",
)
_DEPRECATED_AUTH_CONFIG_KEYS = ("auth.password_hash", "auth.username")


class AccountIdentityCutoverError(RuntimeError):
    """Raised when cutover state is unsafe or verification fails."""


@dataclass(frozen=True)
class AccountIdentity:
    """Validated and hashed account input ready for the SQL transaction."""

    id: uuid.UUID
    name: str
    email: str
    password_hash: str


@dataclass(frozen=True)
class AccountIdentityCutoverEvidence:
    """Durable evidence captured immediately before identity mutation."""

    user_id: uuid.UUID
    name: str
    email: str
    predecessor_checksum: str
    target_checksum: str
    non_auth_row_counts: dict[str, int]
    auth_sessions_before: int
    machines_before: int
    deprecated_auth_config_rows_before: dict[str, int]
    deprecated_auth_config_rows_after: dict[str, int]

    def to_json(self) -> dict[str, object]:
        return {
            "auth_sessions_before": self.auth_sessions_before,
            "deprecated_auth_config_rows_after": self.deprecated_auth_config_rows_after,
            "deprecated_auth_config_rows_before": self.deprecated_auth_config_rows_before,
            "email": self.email,
            "machines_before": self.machines_before,
            "name": self.name,
            "non_auth_row_counts": self.non_auth_row_counts,
            "predecessor_checksum": self.predecessor_checksum,
            "target_checksum": self.target_checksum,
            "user_id": str(self.user_id),
        }


@dataclass(frozen=True)
class AccountIdentityCutoverPreflight:
    """Read-only state captured before credentials are requested."""

    non_auth_row_counts: dict[str, int]
    auth_sessions_before: int
    machines_before: int
    deprecated_auth_config_rows_before: dict[str, int]


def admit_account_identity_campaign(connection: psycopg.Connection[Any]) -> None:
    """Admit the cutover campaign in the same transaction that opens its fence."""
    admit_cutover_campaign(connection, predecessor_checksum=PREDECESSOR_BASELINE_CHECKSUM)


def admit_cutover_campaign(
    connection: psycopg.Connection[Any],
    *,
    predecessor_checksum: str,
) -> None:
    """Safely expand campaign constraints for a known baseline predecessor."""
    _require_receipt(connection, predecessor_checksum)
    for table in ("maintenance_epochs", "destructive_batches"):
        constraint = f"{table}_campaign_check"
        definition_row = connection.execute(
            """
            SELECT pg_get_constraintdef(candidate.oid) AS definition
            FROM pg_constraint AS candidate
            WHERE candidate.conrelid = %s::regclass
              AND candidate.conname = %s
            """,
            (table, constraint),
        ).fetchone()
        if definition_row is None:
            raise AccountIdentityCutoverError(f"Missing required constraint {constraint}")
        definition = str(definition_row["definition"])
        if not _campaign_constraint_is_known(definition):
            raise AccountIdentityCutoverError(
                f"Refusing to replace unexpected constraint {constraint}: {definition}"
            )
        connection.execute(
            sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                sql.Identifier(table),
                sql.Identifier(constraint),
            )
        )
        allowed = sql.SQL(", ").join(sql.Literal(value) for value in _TARGET_CAMPAIGNS)
        connection.execute(
            sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} CHECK (campaign IN ({}))").format(
                sql.Identifier(table),
                sql.Identifier(constraint),
                allowed,
            )
        )


def account_identity_cutover_already_applied(
    database_url: str,
    *,
    batch_id: uuid.UUID,
    target_checksum: str,
) -> bool:
    """Return whether a committed cutover can resume without prompting."""
    with _connect(database_url) as connection:
        checksum = _receipt_checksum(connection)
        if checksum == PREDECESSOR_BASELINE_CHECKSUM:
            return False
        if checksum != target_checksum:
            raise AccountIdentityCutoverError(
                "Account identity cutover requires the exact predecessor or target receipt; "
                f"found {checksum or 'no receipt'}"
            )
        evidence = _load_evidence(connection, batch_id)
        _verify_data_invariants(connection, evidence)
        return True


def preflight_account_identity_cutover(
    database_url: str,
) -> AccountIdentityCutoverPreflight:
    """Validate predecessor state and capture counts before prompting."""
    with _connect(database_url) as connection:
        role_row = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            ("gobby_daemon_runtime",),
        ).fetchone()
        if role_row is None:
            raise AccountIdentityCutoverError(
                "Refusing cutover: PostgreSQL role gobby_daemon_runtime does not exist"
            )
        _require_receipt(connection, PREDECESSOR_BASELINE_CHECKSUM)
        _require_predecessor_shape(connection)
        owner_count = _scalar_count(
            connection,
            "SELECT COUNT(*) AS count FROM machines WHERE owner_user_id IS NOT NULL",
        )
        if owner_count:
            raise AccountIdentityCutoverError(
                f"Refusing cutover: {owner_count} machine owner value(s) already exist"
            )
        return _collect_preflight(connection)


def apply_account_identity_cutover(
    database_url: str,
    *,
    epoch_id: uuid.UUID,
    batch_id: uuid.UUID,
    identity: AccountIdentity,
    preflight: AccountIdentityCutoverPreflight,
    target_checksum: str,
) -> AccountIdentityCutoverEvidence:
    """Apply the complete identity transition in one prompt-free transaction."""
    evidence = AccountIdentityCutoverEvidence(
        user_id=identity.id,
        name=identity.name,
        email=identity.email,
        predecessor_checksum=PREDECESSOR_BASELINE_CHECKSUM,
        target_checksum=target_checksum,
        non_auth_row_counts=preflight.non_auth_row_counts,
        auth_sessions_before=preflight.auth_sessions_before,
        machines_before=preflight.machines_before,
        deprecated_auth_config_rows_before=preflight.deprecated_auth_config_rows_before,
        deprecated_auth_config_rows_after=dict.fromkeys(_DEPRECATED_AUTH_CONFIG_KEYS, 0),
    )
    with _connect(database_url) as connection:
        _lock_public_tables(connection)
        _require_receipt(connection, PREDECESSOR_BASELINE_CHECKSUM)
        _require_predecessor_shape(connection)
        current = _collect_preflight(connection)
        if current != preflight:
            raise AccountIdentityCutoverError(
                "Datastore changed after account identity preflight; restart the campaign"
            )
        _create_users_table(connection)
        connection.execute(
            """
            INSERT INTO users(id, email, name, password_hash)
            VALUES (%s, %s, %s, %s)
            """,
            (identity.id, identity.email, identity.name, identity.password_hash),
        )
        _assign_machine_ownership(connection, identity.id)
        _replace_auth_sessions(connection)
        _retire_deprecated_auth_config_rows(
            connection,
            expected=preflight.deprecated_auth_config_rows_before,
        )
        batch_row = connection.execute(
            """
            UPDATE destructive_batches
            SET intent = intent || %s,
                updated_at = NOW()
            WHERE id = %s
              AND maintenance_epoch_id = %s
              AND campaign = %s
            RETURNING id
            """,
            (
                Jsonb({"account_identity_cutover": evidence.to_json()}),
                batch_id,
                epoch_id,
                ACCOUNT_IDENTITY_CAMPAIGN,
            ),
        ).fetchone()
        if batch_row is None:
            raise AccountIdentityCutoverError(
                f"Cutover batch {batch_id} is missing or belongs to another epoch"
            )
        _verify_data_invariants(connection, evidence)
        updated = connection.execute(
            """
            UPDATE schema_migrations
            SET checksum = %s,
                applied_at = NOW()
            WHERE version = %s
              AND filename = %s
              AND checksum = %s
            RETURNING version
            """,
            (
                target_checksum,
                BASELINE_VERSION,
                f"baseline@{BASELINE_VERSION}",
                PREDECESSOR_BASELINE_CHECKSUM,
            ),
        ).fetchone()
        if updated is None:
            raise AccountIdentityCutoverError("Predecessor receipt changed during cutover")
    return evidence


def verify_account_identity_cutover(
    database_url: str,
    *,
    batch_id: uuid.UUID,
    target_checksum: str,
) -> AccountIdentityCutoverEvidence:
    """Verify receipt, ownership, logout, and exact preserved row counts."""
    with _connect(database_url) as connection:
        _require_receipt(connection, target_checksum)
        evidence = _load_evidence(connection, batch_id)
        if evidence.target_checksum != target_checksum:
            raise AccountIdentityCutoverError(
                "Cutover evidence target checksum does not match this release"
            )
        _verify_data_invariants(connection, evidence)
        return evidence


def _create_users_table(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """
        CREATE TABLE users (
            id uuid NOT NULL,
            email text NOT NULL,
            name text NOT NULL,
            password_hash text NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            updated_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT users_email_check CHECK (btrim(email) <> ''::text),
            CONSTRAINT users_name_check CHECK (btrim(name) <> ''::text)
        )
        """
    )
    connection.execute("ALTER TABLE ONLY users ADD CONSTRAINT users_pkey PRIMARY KEY (id)")
    connection.execute("CREATE UNIQUE INDEX users_email_lower_key ON users (lower(email))")
    connection.execute(
        "GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE users TO gobby_daemon_runtime"
    )


def _assign_machine_ownership(
    connection: psycopg.Connection[Any],
    user_id: uuid.UUID,
) -> None:
    connection.execute("DROP INDEX idx_machines_owner_user_id")
    connection.execute(
        """
        ALTER TABLE machines
        ALTER COLUMN owner_user_id TYPE uuid USING owner_user_id::uuid
        """
    )
    connection.execute("UPDATE machines SET owner_user_id = %s", (user_id,))
    connection.execute("ALTER TABLE machines ALTER COLUMN owner_user_id SET NOT NULL")
    connection.execute(
        """
        ALTER TABLE ONLY machines
        ADD CONSTRAINT machines_owner_user_id_fkey
        FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE RESTRICT
        """
    )
    connection.execute("CREATE INDEX idx_machines_owner_user_id ON machines (owner_user_id)")


def _replace_auth_sessions(connection: psycopg.Connection[Any]) -> None:
    connection.execute("DROP TABLE auth_sessions")
    connection.execute(
        """
        CREATE TABLE auth_sessions (
            id uuid NOT NULL,
            user_id uuid NOT NULL,
            token_hash text NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            expires_at timestamp with time zone NOT NULL,
            remember_me boolean DEFAULT false NOT NULL
        )
        """
    )
    connection.execute(
        "ALTER TABLE ONLY auth_sessions ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (id)"
    )
    connection.execute(
        """
        ALTER TABLE ONLY auth_sessions
        ADD CONSTRAINT auth_sessions_token_hash_key UNIQUE (token_hash)
        """
    )
    connection.execute("CREATE INDEX idx_auth_sessions_expires ON auth_sessions (expires_at)")
    connection.execute("CREATE INDEX idx_auth_sessions_user_id ON auth_sessions (user_id)")
    connection.execute(
        """
        ALTER TABLE ONLY auth_sessions
        ADD CONSTRAINT auth_sessions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        """
    )
    connection.execute(
        "GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE auth_sessions TO gobby_daemon_runtime"
    )


def _retire_deprecated_auth_config_rows(
    connection: psycopg.Connection[Any],
    *,
    expected: dict[str, int],
) -> None:
    deleted = dict.fromkeys(_DEPRECATED_AUTH_CONFIG_KEYS, 0)
    rows = connection.execute(
        """
        DELETE FROM config_store
        WHERE key IN (%s, %s)
        RETURNING key
        """,
        _DEPRECATED_AUTH_CONFIG_KEYS,
    ).fetchall()
    for row in rows:
        deleted[str(row["key"])] += 1
    if deleted != expected:
        raise AccountIdentityCutoverError(
            "Deprecated authentication config changed during credential retirement"
        )


def _require_predecessor_shape(connection: psycopg.Connection[Any]) -> None:
    users = connection.execute("SELECT to_regclass('users') AS table_name").fetchone()
    if users is None or users["table_name"] is not None:
        count = _scalar_count(connection, "SELECT COUNT(*) AS count FROM users")
        raise AccountIdentityCutoverError(
            f"Refusing cutover: predecessor schema unexpectedly contains users ({count} row(s))"
        )
    owner = connection.execute(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'machines'
          AND column_name = 'owner_user_id'
        """
    ).fetchone()
    if owner is None or owner["data_type"] != "text" or owner["is_nullable"] != "YES":
        raise AccountIdentityCutoverError(
            "machines.owner_user_id is not the nullable text predecessor column"
        )
    auth_columns = {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'auth_sessions'
            """
        ).fetchall()
    }
    expected = {"token_hash", "created_at", "expires_at", "remember_me"}
    if auth_columns != expected:
        raise AccountIdentityCutoverError(
            f"auth_sessions predecessor columns differ: {sorted(auth_columns)}"
        )


def _collect_preflight(
    connection: psycopg.Connection[Any],
) -> AccountIdentityCutoverPreflight:
    counts = _collect_non_auth_row_counts(connection)
    return AccountIdentityCutoverPreflight(
        non_auth_row_counts=counts,
        auth_sessions_before=_scalar_count(
            connection, "SELECT COUNT(*) AS count FROM auth_sessions"
        ),
        machines_before=counts.get("machines", 0),
        deprecated_auth_config_rows_before=_collect_deprecated_auth_config_row_counts(connection),
    )


def _collect_non_auth_row_counts(connection: psycopg.Connection[Any]) -> dict[str, int]:
    tables = [
        str(row["tablename"])
        for row in connection.execute(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = current_schema()
            ORDER BY tablename
            """
        ).fetchall()
        if str(row["tablename"]) not in _MUTABLE_COUNT_TABLES
    ]
    counts: dict[str, int] = {}
    for table in tables:
        if table == "config_store":
            counts[table] = _scalar_count(
                connection,
                """
                SELECT COUNT(*) AS count
                FROM config_store
                WHERE key NOT IN (%s, %s)
                """,
                _DEPRECATED_AUTH_CONFIG_KEYS,
            )
        else:
            counts[table] = _scalar_count(
                connection,
                sql.SQL("SELECT COUNT(*) AS count FROM {}").format(sql.Identifier(table)),
            )
    return counts


def _collect_deprecated_auth_config_row_counts(
    connection: psycopg.Connection[Any],
) -> dict[str, int]:
    counts = dict.fromkeys(_DEPRECATED_AUTH_CONFIG_KEYS, 0)
    rows = connection.execute(
        """
        SELECT key, COUNT(*) AS count
        FROM config_store
        WHERE key IN (%s, %s)
        GROUP BY key
        """,
        _DEPRECATED_AUTH_CONFIG_KEYS,
    ).fetchall()
    for row in rows:
        counts[str(row["key"])] = int(row["count"])
    return counts


def _verify_data_invariants(
    connection: psycopg.Connection[Any],
    evidence: AccountIdentityCutoverEvidence,
) -> None:
    user = connection.execute("SELECT id, name, email FROM users ORDER BY id").fetchall()
    expected_user = {
        "id": evidence.user_id,
        "name": evidence.name,
        "email": evidence.email,
    }
    if len(user) != 1 or dict(user[0]) != expected_user:
        raise AccountIdentityCutoverError("Cutover must leave exactly the recorded canonical user")
    wrong_owners = _scalar_count(
        connection,
        "SELECT COUNT(*) AS count FROM machines WHERE owner_user_id <> %s",
        (evidence.user_id,),
    )
    if (
        wrong_owners
        or _scalar_count(connection, "SELECT COUNT(*) AS count FROM machines")
        != evidence.machines_before
    ):
        raise AccountIdentityCutoverError("Machine ownership backfill verification failed")
    if _scalar_count(connection, "SELECT COUNT(*) AS count FROM auth_sessions"):
        raise AccountIdentityCutoverError("Authentication sessions were not invalidated")
    expected_deprecated_rows = dict.fromkeys(_DEPRECATED_AUTH_CONFIG_KEYS, 0)
    if evidence.deprecated_auth_config_rows_after != expected_deprecated_rows:
        raise AccountIdentityCutoverError(
            "Cutover evidence must record zero deprecated authentication config rows"
        )
    deprecated_rows = _collect_deprecated_auth_config_row_counts(connection)
    if deprecated_rows != expected_deprecated_rows:
        raise AccountIdentityCutoverError(
            "Deprecated authentication config rows were not retired: "
            f"expected {expected_deprecated_rows}, observed {deprecated_rows}"
        )
    actual_counts = _collect_non_auth_row_counts(connection)
    if actual_counts != evidence.non_auth_row_counts:
        raise AccountIdentityCutoverError(
            "Non-auth row counts changed during cutover: "
            f"expected {evidence.non_auth_row_counts}, observed {actual_counts}"
        )


def _load_evidence(
    connection: psycopg.Connection[Any],
    batch_id: uuid.UUID,
) -> AccountIdentityCutoverEvidence:
    row = connection.execute(
        """
        SELECT intent
        FROM destructive_batches
        WHERE id = %s
          AND campaign = %s
        """,
        (batch_id, ACCOUNT_IDENTITY_CAMPAIGN),
    ).fetchone()
    if row is None:
        raise AccountIdentityCutoverError(f"Cutover batch {batch_id} does not exist")
    intent = cast(object, row["intent"])
    if not isinstance(intent, dict):
        raise AccountIdentityCutoverError("Cutover batch intent is not an object")
    raw = cast(dict[object, object], intent).get("account_identity_cutover")
    if not isinstance(raw, dict):
        raise AccountIdentityCutoverError("Cutover batch has no durable identity evidence")
    values = cast(dict[object, object], raw)
    try:
        counts_raw = values["non_auth_row_counts"]
        if not isinstance(counts_raw, dict):
            raise TypeError("non_auth_row_counts is not an object")
        counts: dict[str, int] = {}
        for table, count in cast(dict[object, object], counts_raw).items():
            if not isinstance(table, str) or isinstance(count, bool) or not isinstance(count, int):
                raise TypeError("invalid row-count evidence")
            counts[table] = count
        auth_sessions_before = values["auth_sessions_before"]
        machines_before = values["machines_before"]
        deprecated_rows_before = _parse_deprecated_auth_config_row_counts(
            values["deprecated_auth_config_rows_before"],
            field="deprecated_auth_config_rows_before",
        )
        deprecated_rows_after = _parse_deprecated_auth_config_row_counts(
            values["deprecated_auth_config_rows_after"],
            field="deprecated_auth_config_rows_after",
        )
        if (
            isinstance(auth_sessions_before, bool)
            or not isinstance(auth_sessions_before, int)
            or isinstance(machines_before, bool)
            or not isinstance(machines_before, int)
        ):
            raise TypeError("invalid scalar count evidence")
        return AccountIdentityCutoverEvidence(
            user_id=uuid.UUID(str(values["user_id"])),
            name=str(values["name"]),
            email=str(values["email"]),
            predecessor_checksum=str(values["predecessor_checksum"]),
            target_checksum=str(values["target_checksum"]),
            non_auth_row_counts=counts,
            auth_sessions_before=auth_sessions_before,
            machines_before=machines_before,
            deprecated_auth_config_rows_before=deprecated_rows_before,
            deprecated_auth_config_rows_after=deprecated_rows_after,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountIdentityCutoverError("Cutover batch identity evidence is invalid") from exc


def _parse_deprecated_auth_config_row_counts(
    raw: object,
    *,
    field: str,
) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise TypeError(f"{field} is not an object")
    values = cast(dict[object, object], raw)
    if set(values) != set(_DEPRECATED_AUTH_CONFIG_KEYS):
        raise TypeError(f"{field} has unexpected keys")
    counts: dict[str, int] = {}
    for key in _DEPRECATED_AUTH_CONFIG_KEYS:
        count = values[key]
        if isinstance(count, bool) or not isinstance(count, int) or count not in (0, 1):
            raise TypeError(f"{field} has an invalid count")
        counts[key] = count
    return counts


def _lock_public_tables(connection: psycopg.Connection[Any]) -> None:
    tables = [
        sql.Identifier(str(row["tablename"]))
        for row in connection.execute(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = current_schema()
            ORDER BY tablename
            """
        ).fetchall()
    ]
    if tables:
        connection.execute(
            sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(sql.SQL(", ").join(tables))
        )


def _require_receipt(connection: psycopg.Connection[Any], expected_checksum: str) -> None:
    checksum = _receipt_checksum(connection)
    if checksum != expected_checksum:
        raise AccountIdentityCutoverError(
            "Account identity cutover receipt mismatch: "
            f"expected {expected_checksum}, found {checksum or 'no receipt'}"
        )


def _receipt_checksum(connection: psycopg.Connection[Any]) -> str | None:
    row = connection.execute(
        """
        SELECT checksum
        FROM schema_migrations
        WHERE version = %s
          AND filename = %s
        """,
        (BASELINE_VERSION, f"baseline@{BASELINE_VERSION}"),
    ).fetchone()
    return None if row is None else str(row["checksum"])


def _campaign_constraint_is_known(definition: str) -> bool:
    values = tuple(re.findall(r"'([^']+)'::text", definition))
    return values in (_OLD_CAMPAIGNS, _ACCOUNT_IDENTITY_CAMPAIGNS, _TARGET_CAMPAIGNS)


def _scalar_count(
    connection: psycopg.Connection[Any],
    query: str | sql.Composed,
    params: tuple[object, ...] = (),
) -> int:
    row = connection.execute(query, params).fetchone()
    if row is None:
        raise AccountIdentityCutoverError("Count query returned no row")
    return int(row["count"])


def _connect(database_url: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(
        database_url,
        autocommit=False,
        application_name="gobby-account-identity-cutover",
        row_factory=dict_row,
    )
