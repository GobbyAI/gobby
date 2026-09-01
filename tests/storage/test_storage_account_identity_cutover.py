from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from types import TracebackType
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

import gobby.storage.account_identity_cutover as cutover
from gobby.identity import hash_password
from gobby.storage.account_identity_cutover import (
    ACCOUNT_IDENTITY_CAMPAIGN,
    PREDECESSOR_BASELINE_CHECKSUM,
    AccountIdentity,
    AccountIdentityCutoverError,
    account_identity_cutover_already_applied,
    apply_account_identity_cutover,
    preflight_account_identity_cutover,
    verify_account_identity_cutover,
)
from gobby.storage.maintenance_epoch import (
    CAMPAIGNS,
    bind_maintenance_epoch,
    create_destructive_batch,
    open_maintenance_epoch,
)
from gobby.storage.schema_contract import apply_schema, expected_schema_identity
from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.integration


class _FailAfterOwnerUuidConversion:
    def __init__(self, connection: psycopg.Connection[dict[str, Any]]) -> None:
        self._connection = connection

    def __enter__(self) -> _FailAfterOwnerUuidConversion:
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._connection.__exit__(exc_type, exc_value, traceback)

    def execute(
        self,
        query: str | sql.SQL | sql.Composed,
        params: tuple[object, ...] | None = None,
    ) -> psycopg.Cursor[dict[str, Any]]:
        cursor = self._connection.execute(query, params)
        statement = " ".join(str(query).split())
        if "ALTER COLUMN owner_user_id TYPE uuid" in statement:
            raise RuntimeError("injected failure after owner UUID conversion")
        return cursor


@pytest.fixture
def predecessor_database(postgres_database_url: str) -> Iterator[str]:
    with isolated_test_schema(postgres_database_url, "identity") as schema_name:
        apply_schema(postgres_database_url, schema=schema_name)
        parts = urlsplit(postgres_database_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        search_path_option = f"-csearch_path={schema_name}"
        existing_options = query.get("options", "")
        query["options"] = (
            f"{existing_options} {search_path_option}".strip()
            if existing_options
            else search_path_option
        )
        scoped_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
        with psycopg.connect(scoped_url, autocommit=True) as connection:
            _restore_predecessor_shape(connection)
            _seed_predecessor_data(connection)
        yield scoped_url


def _restore_predecessor_shape(connection: psycopg.Connection[object]) -> None:
    connection.execute("DROP TABLE auth_sessions")
    connection.execute(
        """
        CREATE TABLE auth_sessions (
            token_hash text NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            expires_at timestamp with time zone NOT NULL,
            remember_me boolean DEFAULT false NOT NULL
        )
        """
    )
    connection.execute(
        """
        ALTER TABLE ONLY auth_sessions
        ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (token_hash)
        """
    )
    connection.execute("CREATE INDEX idx_auth_sessions_expires ON auth_sessions (expires_at)")
    connection.execute(
        "GRANT SELECT, INSERT, DELETE, UPDATE ON TABLE auth_sessions TO gobby_daemon_runtime"
    )
    connection.execute("ALTER TABLE machines DROP CONSTRAINT machines_owner_user_id_fkey")
    connection.execute("DROP INDEX idx_machines_owner_user_id")
    from gobby.storage.account_identity_cutover import (
        BASELINE_VERSION,
        PREDECESSOR_BASELINE_CHECKSUM,
    )

    connection.execute("DELETE FROM schema_migrations")
    connection.execute(
        """
        INSERT INTO schema_migrations (version, filename, checksum)
        VALUES (%s, %s, %s)
        """,
        (BASELINE_VERSION, f"baseline@{BASELINE_VERSION}", PREDECESSOR_BASELINE_CHECKSUM),
    )
    connection.execute("ALTER TABLE machines ALTER COLUMN owner_user_id DROP NOT NULL")
    connection.execute(
        """
        ALTER TABLE machines
        ALTER COLUMN owner_user_id TYPE text USING owner_user_id::text
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_machines_owner_user_id ON machines (owner_user_id)
        WHERE owner_user_id IS NOT NULL
        """
    )
    connection.execute("DROP TABLE users")
    _replace_campaign_constraint(connection, "maintenance_epochs")
    _replace_campaign_constraint(connection, "destructive_batches")
    connection.execute(
        """
        UPDATE schema_migrations
        SET checksum = %s
        WHERE version = 375 AND filename = 'baseline@375'
        """,
        (PREDECESSOR_BASELINE_CHECKSUM,),
    )


def _replace_campaign_constraint(connection: psycopg.Connection[object], table: str) -> None:
    constraint = f"{table}_campaign_check"
    connection.execute(
        sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
            sql.Identifier(table), sql.Identifier(constraint)
        )
    )
    connection.execute(
        sql.SQL(
            "ALTER TABLE {} ADD CONSTRAINT {} "
            "CHECK (campaign IN ('schema-apply', 'purge', 'reconcile', 'flatten'))"
        ).format(sql.Identifier(table), sql.Identifier(constraint))
    )


def _seed_predecessor_data(connection: psycopg.Connection[object]) -> None:
    connection.execute(
        "INSERT INTO machines(id, hostname) VALUES (%s, 'identity-test-machine')",
        (uuid.uuid4(),),
    )
    connection.execute(
        """
        INSERT INTO auth_sessions(token_hash, expires_at, remember_me)
        VALUES ('obsolete-session', NOW() + INTERVAL '1 hour', TRUE)
        """
    )
    connection.execute(
        """
        INSERT INTO config_store(key, value)
        VALUES
            ('identity.cutover.preserved', 'preserved'),
            ('auth.api_token_hash', 'preserved-api-token-hash'),
            ('auth.password_hash', 'legacy-password-hash'),
            ('auth.username', 'legacy-operator')
        """
    )


def _open_cutover(scoped_url: str) -> tuple[str, uuid.UUID, uuid.UUID]:
    epoch = open_maintenance_epoch(
        scoped_url,
        campaign=ACCOUNT_IDENTITY_CAMPAIGN,
        opened_by="hub-maintenance:account-identity-cutover",
        scope_note="isolated migration test",
    )
    batch = create_destructive_batch(
        scoped_url,
        epoch.id,
        campaign=ACCOUNT_IDENTITY_CAMPAIGN,
        intent={"campaign": ACCOUNT_IDENTITY_CAMPAIGN},
    )
    return bind_maintenance_epoch(scoped_url, epoch.id), epoch.id, batch.id


def _identity() -> AccountIdentity:
    return AccountIdentity(
        id=uuid.uuid4(),
        name="Test Operator",
        email="operator@example.com",
        password_hash=hash_password("correct horse battery staple"),
    )


def _target_checksum() -> str:
    checksum = expected_schema_identity()["baseline_checksum"]
    assert isinstance(checksum, str)
    return checksum


def test_populated_predecessor_cutover_preserves_rows_and_forces_logout(
    predecessor_database: str,
) -> None:
    with psycopg.connect(predecessor_database, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO machines(id, hostname)
            VALUES (%s, 'identity-test-machine-2'), (%s, 'identity-test-machine-3')
            """,
            (uuid.uuid4(), uuid.uuid4()),
        )
        connection.execute(
            """
            INSERT INTO auth_sessions(token_hash, expires_at, remember_me)
            VALUES
                ('obsolete-session-2', NOW() + INTERVAL '1 hour', FALSE),
                ('obsolete-session-3', NOW() + INTERVAL '1 hour', TRUE)
            """
        )
    bound_url, epoch_id, batch_id = _open_cutover(predecessor_database)
    identity = _identity()
    preflight = preflight_account_identity_cutover(bound_url)

    evidence = apply_account_identity_cutover(
        bound_url,
        epoch_id=epoch_id,
        batch_id=batch_id,
        identity=identity,
        preflight=preflight,
        target_checksum=_target_checksum(),
    )
    verified = verify_account_identity_cutover(
        bound_url,
        batch_id=batch_id,
        target_checksum=_target_checksum(),
    )

    assert verified == evidence
    assert evidence.auth_sessions_before == 3
    assert evidence.machines_before == 3
    assert evidence.deprecated_auth_config_rows_before == {
        "auth.password_hash": 1,
        "auth.username": 1,
    }
    assert evidence.deprecated_auth_config_rows_after == {
        "auth.password_hash": 0,
        "auth.username": 0,
    }
    assert account_identity_cutover_already_applied(
        bound_url,
        batch_id=batch_id,
        target_checksum=_target_checksum(),
    )
    with psycopg.connect(bound_url, row_factory=dict_row) as connection:
        user = connection.execute("SELECT id, name, email FROM users").fetchone()
        owners = connection.execute("SELECT owner_user_id FROM machines").fetchall()
        sessions = connection.execute("SELECT COUNT(*) AS count FROM auth_sessions").fetchone()
        preserved = connection.execute(
            """
            SELECT key, value
            FROM config_store
            WHERE key IN (
                'identity.cutover.preserved',
                'auth.api_token_hash',
                'auth.password_hash',
                'auth.username'
            )
            ORDER BY key
            """
        ).fetchall()
        batch = connection.execute(
            "SELECT intent FROM destructive_batches WHERE id = %s", (batch_id,)
        ).fetchone()
    assert user == {"id": identity.id, "name": identity.name, "email": identity.email}
    assert owners == [{"owner_user_id": identity.id}] * 3
    assert sessions == {"count": 0}
    assert preserved == [
        {"key": "auth.api_token_hash", "value": "preserved-api-token-hash"},
        {"key": "identity.cutover.preserved", "value": "preserved"},
    ]
    assert batch is not None
    serialized_intent = json.dumps(batch["intent"], sort_keys=True)
    assert "legacy-operator" not in serialized_intent
    assert "legacy-password-hash" not in serialized_intent


def test_failure_after_credential_retirement_restores_legacy_rows(
    predecessor_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound_url, epoch_id, batch_id = _open_cutover(predecessor_database)
    preflight = preflight_account_identity_cutover(bound_url)

    def fail_after_retirement(
        connection: psycopg.Connection[object],
        _evidence: cutover.AccountIdentityCutoverEvidence,
    ) -> None:
        remaining = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM config_store
            WHERE key IN ('auth.password_hash', 'auth.username')
            """
        ).fetchone()
        assert remaining == {"count": 0}
        raise RuntimeError("injected post-retirement failure")

    monkeypatch.setattr(cutover, "_verify_data_invariants", fail_after_retirement)

    with pytest.raises(RuntimeError, match="injected post-retirement failure"):
        apply_account_identity_cutover(
            bound_url,
            epoch_id=epoch_id,
            batch_id=batch_id,
            identity=_identity(),
            preflight=preflight,
            target_checksum=_target_checksum(),
        )

    with psycopg.connect(bound_url, row_factory=dict_row) as connection:
        legacy_rows = connection.execute(
            """
            SELECT key, value
            FROM config_store
            WHERE key IN ('auth.password_hash', 'auth.username')
            ORDER BY key
            """
        ).fetchall()
        batch = connection.execute(
            "SELECT intent FROM destructive_batches WHERE id = %s", (batch_id,)
        ).fetchone()

    assert legacy_rows == [
        {"key": "auth.password_hash", "value": "legacy-password-hash"},
        {"key": "auth.username", "value": "legacy-operator"},
    ]
    assert batch == {"intent": {"campaign": ACCOUNT_IDENTITY_CAMPAIGN}}


def test_campaign_registry_and_admitted_constraints_have_exact_parity(
    predecessor_database: str,
) -> None:
    bound_url, _epoch_id, _batch_id = _open_cutover(predecessor_database)

    with psycopg.connect(bound_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            """
            SELECT conrelid::regclass::text AS table_name,
                   pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conname IN (
                'maintenance_epochs_campaign_check',
                'destructive_batches_campaign_check'
            )
              AND connamespace = (
                  SELECT oid FROM pg_namespace WHERE nspname = current_schema()
              )
            ORDER BY table_name
            """
        ).fetchall()

    expected = {
        "account-identity-cutover",
        "project-checkout-cutover",
        "schema-apply",
        "purge",
        "reconcile",
        "flatten",
    }
    assert set(CAMPAIGNS) == expected - {"flatten"}
    assert {row["table_name"] for row in rows} == {
        "destructive_batches",
        "maintenance_epochs",
    }
    for row in rows:
        assert set(re.findall(r"'([^']+)'::text", row["definition"])) == expected


def test_transaction_failure_rolls_back_identity_mutation(
    predecessor_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound_url, epoch_id, batch_id = _open_cutover(predecessor_database)
    preflight = preflight_account_identity_cutover(bound_url)

    def fail_after_machine_backfill(_connection: psycopg.Connection[object]) -> None:
        raise RuntimeError("injected cutover failure")

    monkeypatch.setattr(cutover, "_replace_auth_sessions", fail_after_machine_backfill)

    with pytest.raises(RuntimeError, match="injected cutover failure"):
        apply_account_identity_cutover(
            bound_url,
            epoch_id=epoch_id,
            batch_id=batch_id,
            identity=_identity(),
            preflight=preflight,
            target_checksum=_target_checksum(),
        )

    with psycopg.connect(bound_url, row_factory=dict_row) as connection:
        receipt = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 375"
        ).fetchone()
        users = connection.execute("SELECT to_regclass('users') AS relation").fetchone()
        owner = connection.execute("SELECT owner_user_id FROM machines").fetchone()
        sessions = connection.execute("SELECT token_hash FROM auth_sessions").fetchall()
        epoch = connection.execute(
            "SELECT released_at FROM maintenance_epochs WHERE id = %s", (epoch_id,)
        ).fetchone()
    assert receipt == {"checksum": PREDECESSOR_BASELINE_CHECKSUM}
    assert users == {"relation": None}
    assert owner == {"owner_user_id": None}
    assert sessions == [{"token_hash": "obsolete-session"}]
    assert epoch == {"released_at": None}


def test_uuid_column_conversion_failure_rolls_back_ddl_and_remains_fenced(
    predecessor_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound_url, epoch_id, batch_id = _open_cutover(predecessor_database)
    preflight = preflight_account_identity_cutover(bound_url)
    original_connect = cutover._connect

    def connect_with_uuid_conversion_fault(database_url: str) -> _FailAfterOwnerUuidConversion:
        return _FailAfterOwnerUuidConversion(original_connect(database_url))

    monkeypatch.setattr(cutover, "_connect", connect_with_uuid_conversion_fault)

    with pytest.raises(RuntimeError, match="injected failure after owner UUID conversion"):
        apply_account_identity_cutover(
            bound_url,
            epoch_id=epoch_id,
            batch_id=batch_id,
            identity=_identity(),
            preflight=preflight,
            target_checksum=_target_checksum(),
        )

    with psycopg.connect(bound_url, row_factory=dict_row) as connection:
        receipt = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 375"
        ).fetchone()
        users = connection.execute("SELECT to_regclass('users') AS relation").fetchone()
        owner_column = connection.execute(
            """
            SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'machines'
              AND column_name = 'owner_user_id'
            """
        ).fetchone()
        owners = connection.execute("SELECT owner_user_id FROM machines").fetchall()
        sessions = connection.execute("SELECT token_hash FROM auth_sessions").fetchall()
        owner_index = connection.execute(
            "SELECT to_regclass('idx_machines_owner_user_id')::text AS name"
        ).fetchone()
        epoch = connection.execute(
            "SELECT released_at FROM maintenance_epochs WHERE id = %s", (epoch_id,)
        ).fetchone()

    assert receipt == {"checksum": PREDECESSOR_BASELINE_CHECKSUM}
    assert users == {"relation": None}
    assert owner_column == {"data_type": "text", "is_nullable": "YES"}
    assert owners == [{"owner_user_id": None}]
    assert sessions == [{"token_hash": "obsolete-session"}]
    assert owner_index == {"name": "idx_machines_owner_user_id"}
    assert epoch == {"released_at": None}


def test_preflight_refuses_missing_daemon_runtime_role(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MissingRoleConnection:
        def execute(self, query: object, _params: object = None) -> Any:
            if "pg_roles" in str(query):
                return type("Result", (), {"fetchone": staticmethod(lambda: None)})()
            raise AssertionError(f"unexpected query before role check: {query}")

        def __enter__(self) -> _MissingRoleConnection:
            return self

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _tb: TracebackType | None,
        ) -> None:
            return None

    monkeypatch.setattr(cutover, "_connect", lambda _url: _MissingRoleConnection())

    with pytest.raises(AccountIdentityCutoverError, match="gobby_daemon_runtime"):
        preflight_account_identity_cutover("postgresql://unused")


def test_preflight_refuses_existing_machine_owners(predecessor_database: str) -> None:
    bound_url, _epoch_id, _batch_id = _open_cutover(predecessor_database)
    with psycopg.connect(bound_url) as connection:
        connection.execute("UPDATE machines SET owner_user_id = 'unexpected-owner'")

    with pytest.raises(AccountIdentityCutoverError, match="owner value"):
        preflight_account_identity_cutover(bound_url)


def test_preflight_refuses_existing_users_table(predecessor_database: str) -> None:
    bound_url, _epoch_id, _batch_id = _open_cutover(predecessor_database)
    with psycopg.connect(bound_url) as connection:
        connection.execute("CREATE TABLE users (id uuid PRIMARY KEY)")

    with pytest.raises(AccountIdentityCutoverError, match="contains users"):
        preflight_account_identity_cutover(bound_url)


def test_open_refuses_unexpected_predecessor_receipt(predecessor_database: str) -> None:
    with psycopg.connect(predecessor_database) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE version = 375",
            ("0" * 64,),
        )

    with pytest.raises(AccountIdentityCutoverError, match="receipt mismatch"):
        open_maintenance_epoch(
            predecessor_database,
            campaign=ACCOUNT_IDENTITY_CAMPAIGN,
            opened_by="test-account-identity-cutover",
            scope_note="receipt refusal test",
        )
