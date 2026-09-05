"""Exact datastore adapters; production schema changes use the canonical runner.

Credentials are resolved at execution and never written into inventories. Qdrant
ownership requires its gwiki payload namespace plus scope, or a matching hub wiki
scope for empty collections. PostgreSQL and Redis operations name exact objects.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Protocol, cast

import psycopg
from falkordb.graph import Graph
from psycopg import sql
from qdrant_client import QdrantClient, models
from redis import Redis

from gobby.storage.hub.postgres import PostgresHubDatabase
from scripts.wiki_retirement_config import WikiConfiguration, target_kind
from scripts.wiki_retirement_inventory import (
    COLLECTION,
    GRAPH,
    PG_TABLES,
    WIKI_NAME,
    RetirementError,
    StoreTarget,
    canonical,
    sha,
)

REGISTRY_TABLES = {
    "rule": "rule_definitions",
    "variable": "session_variable_defaults",
    "job": "cron_jobs",
}


class RedisCommands(Protocol):
    connection_pool: Any

    def info(self, section: str) -> dict[str, Any]: ...
    def dump(self, key: str) -> bytes | None: ...
    def execute_command(self, *args: str) -> object: ...
    def restore(self, key: str, ttl: int, value: bytes, *, replace: bool) -> object: ...
    def close(self) -> None: ...


class Storage(Protocol):
    def identities(self) -> dict[str, str]: ...
    def inventory(self) -> list[StoreTarget]: ...
    def capture(self, target: StoreTarget) -> bytes | None: ...
    def backup(self, target: StoreTarget) -> bytes: ...
    def delete(self, target: StoreTarget) -> None: ...
    def restore(self, target: StoreTarget, data: bytes) -> None: ...
    def close(self) -> None: ...
    def require_schema_pending(self) -> None: ...
    def require_schema_retired(self) -> None: ...


def record(kind: Any, name: str, data: bytes, count: int, identity: str) -> StoreTarget:
    return StoreTarget(kind=kind, name=name, digest=sha(data), count=count, identity=identity)


def service_identity(url: str, internal_port: int, *, isolated: bool) -> str:
    """Local Docker container identity defeats hostname and published-port aliases."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    local = bool(addresses) and all(ipaddress.ip_address(value).is_loopback for value in addresses)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if local:
        listed = subprocess.run(
            ["docker", "ps", "-q"], check=True, capture_output=True, text=True, timeout=30
        )
        ids = listed.stdout.split()
        if ids:
            result = subprocess.run(
                ["docker", "inspect", *ids], check=True, capture_output=True, text=True, timeout=30
            )
            matches = [
                info
                for info in json.loads(result.stdout)
                if any(
                    str(port) == entry["HostPort"]
                    for entry in (
                        info["NetworkSettings"]["Ports"].get(f"{internal_port}/tcp") or []
                    )
                )
            ]
            if len(matches) == 1:
                info = matches[0]
                if (
                    isolated
                    and (info["Config"].get("Labels") or {}).get("gobby.retirement.role")
                    != "rehearsal"
                ):
                    raise RetirementError(
                        "Isolated service lacks a retirement rehearsal ownership label"
                    )
                return sha(canonical([info["Id"], internal_port]))
    if isolated:
        raise RetirementError("Isolated projections require dedicated labeled local containers")
    return sha(canonical([sorted(addresses), port]))


class Datastores:
    """A single explicitly configured hub and its projection endpoints."""

    def __init__(
        self,
        database_url: str,
        qdrant_url: str,
        falkor_url: str,
        *,
        qdrant_key: str | None = None,
        schema: str = "public",
        isolated: bool = False,
    ) -> None:
        self.database_url = database_url
        self.schema = schema
        self.pg = psycopg.connect(database_url, connect_timeout=10)
        self.qdrant = QdrantClient(url=qdrant_url, api_key=qdrant_key, timeout=30)
        self.redis = cast(
            RedisCommands, Redis.from_url(falkor_url, socket_timeout=30, socket_connect_timeout=10)
        )
        self.qdrant_url = qdrant_url
        self.falkor_url = falkor_url
        self.isolated = isolated
        self._identities: dict[str, str] | None = None
        self.configuration = WikiConfiguration(database_url, schema, self.pg)

    def close(self) -> None:
        self.pg.close()
        self.qdrant.close()
        self.redis.close()

    def identities(self) -> dict[str, str]:
        # Stable endpoint identities deliberately exclude secrets and process run IDs.
        row = self.pg.execute(
            "SELECT current_database(), oid::text, (pg_control_system()).system_identifier::text "
            "FROM pg_database WHERE datname = current_database()"
        ).fetchone()
        if row is None:
            raise RetirementError("Cannot identify PostgreSQL database")
        self.pg.rollback()
        self.qdrant.info()
        self.redis.info("server")
        self._identities = {
            "postgres": sha(canonical([*row, self.schema])),
            "qdrant": service_identity(self.qdrant_url, 6333, isolated=self.isolated),
            "falkor": service_identity(self.falkor_url, 6379, isolated=self.isolated),
        }
        return self._identities

    def checkouts(self) -> list[Path]:
        rows = self.pg.execute("SELECT DISTINCT root_path FROM project_checkouts").fetchall()
        self.pg.rollback()
        return [Path(row[0]) for row in rows if Path(row[0]).is_dir()]

    def _table_exists(self, name: str) -> bool:
        row = self.pg.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s)",
            (self.schema, name),
        ).fetchone()
        if row is None:
            raise RetirementError("PostgreSQL existence query returned no row")
        return bool(row[0])

    def _table_rows(self, name: str, row_id: str | None = None) -> list[dict[str, Any]]:
        if name not in {*PG_TABLES, *REGISTRY_TABLES.values(), "cron_runs"}:
            raise RetirementError("Unowned PostgreSQL table")
        query = sql.SQL("SELECT to_jsonb(t) FROM {}.{} t").format(
            sql.Identifier(self.schema), sql.Identifier(name)
        )
        params: tuple[str, ...] = ()
        if row_id is not None:
            column = "cron_job_id" if name == "cron_runs" else "id"
            query += sql.SQL(" WHERE {} = %s").format(sql.Identifier(column))
            params = (row_id,)
        rows = [row[0] for row in self.pg.execute(query, params)]
        return sorted(rows, key=lambda row: canonical(row))

    def _pg_snapshot(self) -> bytes:
        tables = {}
        for table in PG_TABLES:
            if self._table_exists(table):
                columns = self.pg.execute(
                    "SELECT column_name, data_type, udt_name, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                    "ORDER BY ordinal_position",
                    (self.schema, table),
                ).fetchall()
                tables[table] = {"columns": columns, "rows": self._table_rows(table)}
        self.pg.rollback()
        return canonical(tables)

    def _registry_snapshot(self, kind: str, name: str) -> bytes | None:
        rows = self._table_rows(REGISTRY_TABLES[kind], name)
        if not rows:
            self.pg.rollback()
            return None
        row = rows[0]
        if not WIKI_NAME.search(str(row.get("name", ""))):
            raise RetirementError(f"Registry target lacks wiki ownership: {kind}:{name}")
        data: dict[str, Any] = {"row": row}
        if kind == "job":
            data["runs"] = self._table_rows("cron_runs", name)
        self.pg.rollback()
        return canonical(data)

    def _qdrant_snapshot(self, name: str) -> bytes | None:
        if not COLLECTION.fullmatch(name):
            raise RetirementError(f"Collection is outside wiki scope: {name}")
        if not self.qdrant.collection_exists(name):
            return None
        info = self.qdrant.get_collection(name)
        points: list[dict[str, Any]] = []
        offset = None
        while True:
            batch, offset = self.qdrant.scroll(
                name, offset=offset, limit=256, with_payload=True, with_vectors=True
            )
            points.extend(point.model_dump(mode="json") for point in batch)
            if offset is None:
                break
        points.sort(key=lambda point: str(point["id"]))
        return canonical(
            {
                "config": info.config.model_dump(mode="json"),
                "payload_schema": {
                    key: value.model_dump(mode="json") for key, value in info.payload_schema.items()
                },
                "points": points,
            }
        )

    def _wiki_scopes(self) -> set[tuple[str, str]]:
        scopes = set()
        for table in PG_TABLES:
            if self._table_exists(table):
                rows = self.pg.execute(
                    sql.SQL("SELECT DISTINCT scope_kind, scope_id FROM {}.{}").format(
                        sql.Identifier(self.schema), sql.Identifier(table)
                    )
                ).fetchall()
                scopes.update(rows)
        self.pg.rollback()
        return scopes

    def inventory(self) -> list[StoreTarget]:
        identities = self.identities()
        data = self._pg_snapshot()
        pg_value = json.loads(data)
        targets = [
            record(
                "postgres",
                "wiki_schema",
                data,
                sum(len(value["rows"]) for value in pg_value.values()),
                identities["postgres"],
            )
        ]
        scopes = self._wiki_scopes()
        targets.extend(self.configuration.inventory(identities["postgres"]))
        for kind, table in REGISTRY_TABLES.items():
            for row in self._table_rows(table):
                if WIKI_NAME.search(str(row.get("name", ""))):
                    snapshot = self._registry_snapshot(kind, str(row["id"]))
                    if snapshot is None:
                        raise RetirementError("Registry changed during inventory")
                    targets.append(
                        record(kind, str(row["id"]), snapshot, 1, identities["postgres"])
                    )
        for collection in self.qdrant.get_collections().collections:
            name = collection.name
            if not COLLECTION.fullmatch(name):
                continue
            collection_data = self._qdrant_snapshot(name)
            if collection_data is None:
                raise RetirementError("Collection changed during inventory")
            points = json.loads(collection_data)["points"]
            _, kind, value = name.split("_", 2)
            if not points and (kind, value) not in scopes:
                raise RetirementError(
                    f"Empty wiki-like collection needs ownership evidence: {name}"
                )
            for point in points:
                payload = point.get("payload") or {}
                if (
                    payload.get("namespace"),
                    payload.get("scope_kind"),
                    payload.get("scope_id"),
                ) != ("gwiki", kind, value):
                    raise RetirementError(f"Collection contains unowned points: {name}")
            targets.append(
                record("qdrant", name, collection_data, len(points), identities["qdrant"])
            )
        graph_data = self._falkor_snapshot()
        if graph_data is not None:
            targets.append(record("falkor", GRAPH, graph_data, 1, identities["falkor"]))
        return targets

    def _falkor_snapshot(self) -> bytes | None:
        """Canonical graph meaning; RDB bytes are opaque recovery data, not identity."""
        if not self.redis.execute_command("EXISTS", GRAPH):
            return None
        graph = Graph(self.redis, GRAPH)
        queries = {
            "nodes": "MATCH (n) RETURN id(n) AS id, labels(n) AS labels, properties(n) AS properties",
            "edges": "MATCH (a)-[r]->(b) RETURN id(r) AS id, id(a) AS source, id(b) AS target, type(r) AS type, properties(r) AS properties",
            "labels": "CALL db.labels()",
            "relationships": "CALL db.relationshipTypes()",
            "properties": "CALL db.propertyKeys()",
            "indexes": "CALL db.indexes()",
            "constraints": "CALL db.constraints()",
        }
        snapshot = {}
        for name, query in queries.items():
            result = graph.ro_query(query, timeout=30000)
            columns = [
                column[1].decode() if isinstance(column[1], bytes) else column[1]
                for column in result.header
            ]
            rows = []
            for values in result.result_set:
                row = dict(zip(columns, values, strict=True))
                if name == "nodes":
                    row["labels"] = sorted(row["labels"])
                if name in {"indexes", "constraints"}:
                    # Build progress and index telemetry are not schema declarations.
                    row.pop("status", None)
                    row.pop("info", None)
                rows.append(row)
            snapshot[name] = sorted(rows, key=canonical)
        return canonical(snapshot)

    def capture(self, target: StoreTarget) -> bytes | None:
        if target.kind in {"config", "discovery"}:
            return self.configuration.capture(target_kind(target), target.name)
        if target.kind == "postgres":
            data = self._pg_snapshot()
            return None if data == b"{}" else data
        if target.kind in REGISTRY_TABLES:
            return self._registry_snapshot(target.kind, target.name)
        if target.kind == "qdrant":
            return self._qdrant_snapshot(target.name)
        if target.name != GRAPH:
            raise RetirementError("Unexpected graph target")
        return self._falkor_snapshot()

    def _pg_command(self, tool: str, *args: str, payload: bytes | None = None) -> bytes:
        from gobby.cli.hub_backup._stores import _postgres_client_command
        from gobby.cli.postgres_backup import _dsn_db, _dsn_user, _managed_postgres_container

        container = (
            _managed_postgres_container(self.database_url)
            if not self.isolated
            else self._isolated_container()
        )
        command = _postgres_client_command(
            tool,
            "-U",
            (_dsn_user(self.database_url) or "gobby"),
            "-d",
            (_dsn_db(self.database_url) or "gobby"),
            *args,
            interactive=payload is not None,
            container=container,
        )
        result = subprocess.run(
            command, input=payload, capture_output=True, timeout=300, check=False
        )
        if result.returncode:
            # pg tools may echo credentials or connection options; retain only the return code.
            raise RetirementError(f"Scoped {tool} failed with exit status {result.returncode}")
        return result.stdout

    def _isolated_container(self) -> str:
        from urllib.parse import urlparse

        container = os.environ.get("GOBBY_RETIRE_ISOLATED_POSTGRES_CONTAINER")
        if not container:
            raise RetirementError(
                "Set GOBBY_RETIRE_ISOLATED_POSTGRES_CONTAINER for scoped pg tools"
            )
        result = subprocess.run(
            ["docker", "inspect", container], capture_output=True, check=True, text=True, timeout=30
        )
        info = json.loads(result.stdout)[0]
        labels = info["Config"].get("Labels") or {}
        ports = info["NetworkSettings"]["Ports"].get("5432/tcp") or []
        dsn = urlparse(self.database_url)
        if (
            labels.get("gobby.retirement.role") != "rehearsal"
            or dsn.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not any(str(dsn.port) == port["HostPort"] for port in ports)
        ):
            raise RetirementError("Isolated PostgreSQL container identity/port does not match")
        return container

    def backup(self, target: StoreTarget) -> bytes:
        data = self.capture(target)
        if target.kind == "postgres":
            data = self._pg_snapshot()
        if data is None or sha(data) != target.digest:
            raise RetirementError(f"Datastore changed since inventory: {target.key}")
        if target.kind == "falkor":
            archive = self.redis.dump(GRAPH)
            if not isinstance(archive, bytes):
                raise RetirementError("FalkorDB DUMP did not return recovery bytes")
            if self._falkor_snapshot() != data:
                raise RetirementError("FalkorDB changed during backup")
            return canonical(
                {"snapshot": json.loads(data), "archive": base64.b64encode(archive).decode()}
            )
        if target.kind != "postgres":
            return data
        present = json.loads(data)
        if not present:
            return canonical({"snapshot": {}, "archive": ""})
        archive = self._pg_command(
            "pg_dump",
            "-Fc",
            "--strict-names",
            *(
                f'--table="{self.schema}"."{name}"'
                for name in PG_TABLES
                if name in json.loads(data)
            ),
        )
        if sha(self._pg_snapshot()) != target.digest:
            raise RetirementError("PostgreSQL changed during backup")
        return canonical(
            {"snapshot": json.loads(data), "archive": base64.b64encode(archive).decode()}
        )

    def schema_head(self) -> int:
        row = self.pg.execute(
            sql.SQL("SELECT MAX(version) FROM {}.schema_migrations").format(
                sql.Identifier(self.schema)
            )
        ).fetchone()
        self.pg.rollback()
        if row is None or row[0] is None:
            raise RetirementError("Canonical schema lineage is missing")
        return int(row[0])

    def require_schema_pending(self) -> None:
        from gobby.storage.schema_contract import expected_schema_identity

        if expected_schema_identity().get("latest_version") != 426 or self.schema_head() != 425:
            raise RetirementError("Retirement admits exactly the canonical 425 to 426 migration")

    def require_schema_retired(self) -> None:
        if self.schema_head() != 426 or self._pg_snapshot() != b"{}":
            raise RetirementError("Canonical schema 426 and exact five-table absence are required")

    def delete(self, target: StoreTarget) -> None:
        if target.kind in {"config", "discovery"}:
            self.configuration.delete(target)
            return
        if target.kind == "postgres":
            from gobby.storage.schema_contract import apply_schema

            apply_schema(self.database_url, schema=self.schema, destructive=True)
            return
        if target.kind == "qdrant":
            if not COLLECTION.fullmatch(target.name):
                raise RetirementError("Unexpected collection target")
            self.qdrant.delete_collection(target.name)
            return
        if target.kind == "falkor":
            if target.name != GRAPH:
                raise RetirementError("Unexpected graph target")
            self.redis.execute_command("GRAPH.DELETE", GRAPH)
            return
        db = PostgresHubDatabase(self.database_url)
        try:
            table = REGISTRY_TABLES[target.kind]
            with db.transaction() as txn:
                # Managers reuse this transaction, so compare and deletion share the
                # same row locks. Parent FOR UPDATE also fences new cron-run inserts.
                txn.execute(
                    sql.SQL("SET LOCAL search_path TO {}, pg_catalog")
                    .format(sql.Identifier(self.schema))
                    .as_string(self.pg)
                )
                row = txn.execute(
                    sql.SQL("SELECT to_jsonb(t) AS snapshot FROM {} t WHERE id=%s FOR UPDATE")
                    .format(sql.Identifier(table))
                    .as_string(self.pg),
                    (target.name,),
                ).fetchone()
                if row is None:
                    raise RetirementError(
                        f"Registry disappeared before locked deletion: {target.key}"
                    )
                # Hub cursors normalize JSONB to serialized JSON for model decoders.
                snapshot: dict[str, Any] = {"row": json.loads(row["snapshot"])}
                if target.kind == "job":
                    runs = txn.execute(
                        "SELECT to_jsonb(t) AS snapshot FROM cron_runs t WHERE cron_job_id=%s FOR UPDATE",
                        (target.name,),
                    ).fetchall()
                    snapshot["runs"] = sorted(
                        [json.loads(item["snapshot"]) for item in runs], key=canonical
                    )
                if sha(canonical(snapshot)) != target.digest:
                    raise RetirementError(f"Registry changed before locked deletion: {target.key}")
                if target.kind == "rule":
                    from gobby.storage.definitions.rules import RuleDefinitionManager

                    RuleDefinitionManager(db).hard_delete(target.name)
                elif target.kind == "variable":
                    from gobby.storage.definitions.variables import SessionVariableDefaultManager

                    SessionVariableDefaultManager(db).hard_delete(target.name)
                else:
                    from gobby.storage.cron import CronJobStorage

                    CronJobStorage(db).delete_project_jobs([target.name])
        finally:
            db.close()

    def restore(self, target: StoreTarget, data: bytes) -> None:
        if not self.isolated:
            raise RetirementError("Restoration requires explicit isolated connections")
        current = self.capture(target)
        if current is not None and sha(current) == target.digest:
            return
        if target.kind in {"config", "discovery"}:
            self.configuration.restore(target, data)
            return
        if target.kind == "postgres":
            value = json.loads(data)
            data_only = False
            if current is not None:
                existing = json.loads(current)
                expected = value["snapshot"]
                if set(existing) != set(expected) or any(
                    table["rows"] or table["columns"] != expected[name]["columns"]
                    for name, table in existing.items()
                ):
                    raise RetirementError(
                        "PostgreSQL restore requires absent or identical empty wiki tables"
                    )
                data_only = True
            archive = base64.b64decode(value["archive"], validate=True)
            if archive:
                extra = ("--data-only", "--disable-triggers") if data_only else ()
                self._pg_command(
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                    *extra,
                    payload=archive,
                )
        elif target.kind == "qdrant":
            if current is not None:
                raise RetirementError(f"Restore target already exists: {target.key}")
            value = json.loads(data)
            config = models.CollectionConfig.model_validate(value["config"])
            params = config.params
            self.qdrant.create_collection(
                target.name,
                vectors_config=params.vectors,
                sparse_vectors_config=params.sparse_vectors,
                shard_number=params.shard_number,
                replication_factor=params.replication_factor,
                write_consistency_factor=params.write_consistency_factor,
                on_disk_payload=params.on_disk_payload,
                hnsw_config=models.HnswConfigDiff.model_validate(config.hnsw_config.model_dump()),
                optimizers_config=models.OptimizersConfigDiff.model_validate(
                    config.optimizer_config.model_dump()
                ),
                wal_config=(
                    models.WalConfigDiff.model_validate(config.wal_config.model_dump())
                    if config.wal_config
                    else None
                ),
                quantization_config=config.quantization_config,
            )
            for key, field in value["payload_schema"].items():
                self.qdrant.create_payload_index(
                    target.name,
                    key,
                    field_schema=field.get("params") or field["data_type"],
                    wait=True,
                )
            points = value["points"]
            for offset in range(0, len(points), 256):
                self.qdrant.upsert(
                    target.name,
                    points=[
                        models.PointStruct(
                            id=point["id"], vector=point["vector"], payload=point["payload"]
                        )
                        for point in points[offset : offset + 256]
                    ],
                    wait=True,
                )
        elif target.kind == "falkor":
            if current is not None:
                raise RetirementError(f"Restore target already exists: {target.key}")
            value = json.loads(data)
            if sha(canonical(value["snapshot"])) != target.digest:
                raise RetirementError("FalkorDB recovery snapshot differs from inventory")
            self.redis.restore(
                GRAPH, 0, base64.b64decode(value["archive"], validate=True), replace=False
            )
            if self._falkor_snapshot() != canonical(value["snapshot"]):
                raise RetirementError("Restored FalkorDB graph differs from inventoried meaning")
        else:
            if current is not None:
                raise RetirementError(f"Restore target already exists: {target.key}")
            value = json.loads(data)
            if sha(canonical(value)) != target.digest or value["row"].get("id") != target.name:
                raise RetirementError("Registry recovery data differs from the exact target")
            if not WIKI_NAME.search(str(value["row"].get("name", ""))):
                raise RetirementError("Registry recovery data lacks wiki ownership")
            runs = value.get("runs", []) if target.kind == "job" else []
            if any(row.get("cron_job_id") != target.name for row in runs):
                raise RetirementError("Cron recovery contains an unrelated execution")
            db = PostgresHubDatabase(self.database_url)
            try:
                with db.transaction() as txn:
                    txn.execute(
                        sql.SQL("SET LOCAL search_path TO {}, pg_catalog")
                        .format(sql.Identifier(self.schema))
                        .as_string(self.pg)
                    )
                    self._prepare_registry_references(db, value["row"], runs)
                    self._insert_row(db, REGISTRY_TABLES[target.kind], value["row"])
                    for row in runs:
                        self._insert_row(db, "cron_runs", row)
            finally:
                db.close()

    def _prepare_registry_references(
        self, db: PostgresHubDatabase, row: dict[str, Any], runs: list[dict[str, Any]]
    ) -> None:
        """Create only missing identity stubs in the isolated recovery transaction."""
        if not self.isolated:
            raise RetirementError("Reference preparation requires isolated restoration")
        import secrets
        from uuid import NAMESPACE_URL, UUID, uuid5

        from gobby.identity import hash_password
        from gobby.storage.machines import LocalMachineManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.users import LocalUserManager

        projects = LocalProjectManager(db)
        project_id = row.get("project_id")
        if project_id is not None:
            project_id = str(UUID(project_id))
            if projects.get(project_id) is None:
                projects.create(f"wiki-retirement-{project_id}", project_id=project_id)
        machines = LocalMachineManager(db)
        users = LocalUserManager(db)
        for machine_id in sorted({str(UUID(run["machine_id"])) for run in runs}):
            if machines.get(machine_id) is not None:
                continue
            owner_id = str(
                uuid5(NAMESPACE_URL, f"gobby:wiki-retirement:machine-owner:{machine_id}")
            )
            if users.get(owner_id) is None:
                users.create(
                    user_id=owner_id,
                    name="Wiki retirement rehearsal",
                    email=f"wiki-retirement-{machine_id}@example.invalid",
                    password_hash=hash_password(secrets.token_urlsafe(32)),
                )
            machines.upsert_seen(machine_id, owner_id, label="Wiki retirement rehearsal")

    def _insert_row(self, db: PostgresHubDatabase, table: str, row: dict[str, Any]) -> None:
        if table not in {*REGISTRY_TABLES.values(), "cron_runs"}:
            raise RetirementError("Unowned restore table")
        query = sql.SQL(
            "INSERT INTO {}.{} SELECT * FROM jsonb_populate_record(NULL::{}.{}, %s::jsonb)"
        ).format(
            sql.Identifier(self.schema),
            sql.Identifier(table),
            sql.Identifier(self.schema),
            sql.Identifier(table),
        )
        db.execute(query.as_string(self.pg), (canonical(row).decode(),))


def configured_stores(*, isolated: bool = False) -> Datastores:
    """Connection secrets are supplied by env; normal hub URL uses its bootstrap."""
    prefix = "GOBBY_RETIRE_ISOLATED_" if isolated else "GOBBY_RETIRE_"
    database_url = os.environ.get(prefix + "DATABASE_URL")
    if not database_url and not isolated:
        from gobby.config.bootstrap import load_bootstrap

        database_url = load_bootstrap(resolve_database_url=True).database_url
    qdrant_url = os.environ.get(prefix + "QDRANT_URL")
    falkor_url = os.environ.get(prefix + "FALKOR_URL")
    if not database_url or not qdrant_url or not falkor_url:
        raise RetirementError(
            f"Set {prefix}DATABASE_URL, {prefix}QDRANT_URL and {prefix}FALKOR_URL"
        )
    return Datastores(
        database_url,
        qdrant_url,
        falkor_url,
        qdrant_key=os.environ.get(prefix + "QDRANT_API_KEY"),
        isolated=isolated,
    )
