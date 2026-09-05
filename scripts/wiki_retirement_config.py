"""Exact wiki configuration recovery using the canonical revision contract.

The active registry no longer accepts these keys. This retirement-only adapter
compares archived rows under its revision lock without a runtime compatibility path.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import psycopg
from psycopg import sql

from gobby.config.registry_key_encoding import decode_dynamic_segment
from gobby.storage.config_mutations import CONFIG_CHANGED_CHANNEL
from gobby.storage.config_repository import MAX_CONFIG_REVISION, ConfigRepository
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import Transaction
from scripts.wiki_retirement_inventory import RetirementError, StoreTarget, canonical, sha

# Exact ownership from the archived WikiConfig and its single mapping pattern.
WIKI_KEYS = frozenset(
    f"wiki.{name}"
    for name in (
        "enabled",
        "roots",
        "debounce_interval",
        "poll_interval",
        "ignore_globs",
        "codewiki_on_commit",
        "codewiki_nightly_enabled",
        "codewiki_nightly_schedule_cron",
        "codewiki_nightly_timezone",
        "codewiki_scopes",
    )
)
PROJECT_SCOPES = "wiki.codewiki_project_scopes_by_name."
WIKI_TOOLS = frozenset(
    f"wiki_{name}"
    for name in (
        "search",
        "ask",
        "read",
        "attach",
        "ingest",
        "write_page",
        "delete_page",
        "compile",
        "audit",
        "trust",
        "health",
        "sync_sessions",
        "list_sources",
        "remove_source",
    )
)
Kind = Literal["config", "discovery"]
TARGETS: dict[Kind, tuple[str, str]] = {
    "config": ("wiki_configuration", "config_store"),
    "discovery": ("gobby-wiki", "tool_schema_hashes"),
}


def owned_config_key(key: str) -> bool:
    if key in WIKI_KEYS:
        return True
    if key.startswith(PROJECT_SCOPES):
        try:
            decode_dynamic_segment(key.removeprefix(PROJECT_SCOPES))
        except ValueError as exc:
            raise RetirementError("Noncanonical legacy wiki configuration key") from exc
        return True
    if key == "wiki" or key.startswith("wiki."):
        raise RetirementError(f"Unsupported legacy wiki configuration key: {key}")
    return False


def validate_rows(kind: Kind, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if kind == "config":
            if not owned_config_key(str(row["key"])) or row["is_secret"]:
                raise RetirementError("Configuration snapshot lacks exact wiki ownership")
        elif row["server_name"] != "gobby-wiki" or row["tool_name"] not in WIKI_TOOLS:
            raise RetirementError("Discovery snapshot lacks exact legacy tool ownership")


def target_kind(target: StoreTarget) -> Kind:
    if target.kind not in {"config", "discovery"}:
        raise RetirementError("Unexpected configuration/discovery target")
    kind: Kind = "config" if target.kind == "config" else "discovery"
    if TARGETS[kind][0] != target.name:
        raise RetirementError("Unexpected configuration/discovery target name")
    return kind


class WikiConfiguration:
    def __init__(self, database_url: str, schema: str, pg: psycopg.Connection[Any]) -> None:
        self.database_url = database_url
        self.schema = schema
        self.pg = pg

    def _table(self, kind: Kind) -> sql.Composed:
        return sql.SQL("{}.{}").format(
            sql.Identifier(self.schema), sql.Identifier(TARGETS[kind][1])
        )

    def _select(self, kind: Kind, *, lock: bool = False) -> sql.Composed:
        predicate = (
            sql.SQL("key = 'wiki' OR starts_with(key, 'wiki.')")
            if kind == "config"
            else sql.SQL("server_name = 'gobby-wiki'")
        )
        return sql.SQL("SELECT to_jsonb(t) AS snapshot FROM {} t WHERE {}{}").format(
            self._table(kind), predicate, sql.SQL(" FOR UPDATE" if lock else "")
        )

    def capture(self, kind: Kind, name: str) -> bytes | None:
        if TARGETS[kind][0] != name:
            raise RetirementError("Unexpected configuration/discovery target")
        exists = self.pg.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s)",
            (self.schema, TARGETS[kind][1]),
        ).fetchone()
        rows = (
            []
            if not exists or not exists[0]
            else [row[0] for row in self.pg.execute(self._select(kind))]
        )
        self.pg.rollback()
        validate_rows(kind, rows)
        return canonical(sorted(rows, key=canonical)) if rows else None

    def inventory(self, identity: str) -> list[StoreTarget]:
        targets = []
        for kind, (name, _) in TARGETS.items():
            data = self.capture(kind, name)
            if data is not None:
                targets.append(
                    StoreTarget(
                        kind=kind,
                        name=name,
                        digest=sha(data),
                        count=len(json.loads(data)),
                        identity=identity,
                    )
                )
        # Internal registration was code-owned. Persisted servers/templates need
        # separate ownership evidence, never deletion from a name match.
        for table in ("mcp_servers", "mcp_server_templates"):
            exists = self.pg.execute(
                "SELECT to_regclass(%s)", (f"{self.schema}.{table}",)
            ).fetchone()
            if exists and exists[0]:
                found = self.pg.execute(
                    sql.SQL(
                        "SELECT 1 FROM {}.{} WHERE name IN ('gobby-wiki','gwiki','wiki') LIMIT 1"
                    ).format(sql.Identifier(self.schema), sql.Identifier(table))
                ).fetchone()
                if found:
                    self.pg.rollback()
                    raise RetirementError("Persisted wiki MCP registration needs ownership review")
        self.pg.rollback()
        return targets

    def _locked_rows(self, tx: Transaction, kind: Kind) -> list[dict[str, Any]]:
        rows = tx.execute(self._select(kind, lock=True).as_string(self.pg)).fetchall()
        return sorted([json.loads(row["snapshot"]) for row in rows], key=canonical)

    def _bind(self, tx: Transaction) -> None:
        tx.execute(
            sql.SQL("SET LOCAL search_path TO {}, pg_catalog")
            .format(sql.Identifier(self.schema))
            .as_string(self.pg)
        )

    @staticmethod
    def _advance(tx: Transaction, revision: int) -> None:
        if revision >= MAX_CONFIG_REVISION:
            raise RetirementError("Configuration revision exhausted")
        tx.execute("UPDATE config_state SET revision=%s WHERE id=%s", (revision + 1, True))
        tx.execute("SELECT pg_notify(%s,%s)", (CONFIG_CHANGED_CHANNEL, str(revision + 1)))

    def delete(self, target: StoreTarget) -> None:
        kind = target_kind(target)
        db = PostgresHubDatabase(self.database_url)
        try:
            with db.transaction() as tx:
                self._bind(tx)
                if kind == "config":
                    revision = ConfigRepository(db).read_revision(tx, lock=True)
                else:
                    # Fence inserts while comparing this exact server's row set.
                    tx.execute(
                        sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE")
                        .format(self._table(kind))
                        .as_string(self.pg)
                    )
                    revision = None
                rows = self._locked_rows(tx, kind)
                validate_rows(kind, rows)
                if sha(canonical(rows)) != target.digest:
                    raise RetirementError("Configuration/discovery changed before locked deletion")
                column = "key" if kind == "config" else "id"
                for row in rows:
                    tx.execute(
                        sql.SQL("DELETE FROM {} WHERE {}=%s")
                        .format(self._table(kind), sql.Identifier(column))
                        .as_string(self.pg),
                        (row[column],),
                    )
                if revision is not None:
                    self._advance(tx, revision)
        finally:
            db.close()

    def restore(self, target: StoreTarget, data: bytes) -> None:
        kind = target_kind(target)
        rows = json.loads(data)
        if sha(canonical(rows)) != target.digest:
            raise RetirementError("Configuration/discovery recovery differs from inventory")
        validate_rows(kind, rows)
        db = PostgresHubDatabase(self.database_url)
        try:
            with db.transaction() as tx:
                self._bind(tx)
                revision = ConfigRepository(db).read_revision(tx, lock=True)
                if self._locked_rows(tx, kind):
                    raise RetirementError("Configuration/discovery restore target already exists")
                if kind == "discovery":
                    from gobby.storage.projects import LocalProjectManager

                    projects = LocalProjectManager(db)
                    for project_id in {row["project_id"] for row in rows} - {None}:
                        if projects.get(project_id) is None:
                            projects.create(f"wiki-retirement-{project_id}", project_id=project_id)
                for row in rows:
                    tx.execute(
                        sql.SQL(
                            "INSERT INTO {}{} SELECT * FROM jsonb_populate_record(NULL::{}, %s::jsonb)"
                        )
                        .format(
                            self._table(kind),
                            sql.SQL(" OVERRIDING SYSTEM VALUE" if kind == "discovery" else ""),
                            self._table(kind),
                        )
                        .as_string(self.pg),
                        (canonical(row).decode(),),
                    )
                if kind == "config":
                    self._advance(tx, max(revision, *(int(row["revision"]) for row in rows)))
                else:
                    tx.execute(
                        "SELECT setval(pg_get_serial_sequence('tool_schema_hashes','id'), "
                        "GREATEST((SELECT MAX(id) FROM tool_schema_hashes), "
                        "nextval(pg_get_serial_sequence('tool_schema_hashes','id'))))"
                    )
        finally:
            db.close()
