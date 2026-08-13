"""Isolated schema fixtures for typed definition-manager tests."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from gobby.storage.definitions.revisions import reset_definition_revision_state
from gobby.storage.hub.postgres import PostgresHubDatabase

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS definition_revisions (
    domain text PRIMARY KEY,
    revision bigint DEFAULT 0 NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    sources jsonb,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT rule_definitions_source_check CHECK (
        (source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text]))
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_defs_live_name
    ON rule_definitions USING btree (name, project_id) NULLS NOT DISTINCT
    WHERE (deleted_at IS NULL);

CREATE TABLE IF NOT EXISTS session_variable_defaults (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    default_value jsonb,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT session_variable_defaults_source_check CHECK (
        (source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text]))
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_var_defs_live_name
    ON session_variable_defaults USING btree (name, project_id) NULLS NOT DISTINCT
    WHERE (deleted_at IS NULL);

CREATE TABLE IF NOT EXISTS pipeline_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    version text DEFAULT '1.0'::text NOT NULL,
    definition_json jsonb NOT NULL,
    canvas_json jsonb,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT pipeline_definitions_source_check CHECK (
        (source = ANY (ARRAY['installed'::text, 'custom'::text, 'project'::text]))
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_defs_live_name
    ON pipeline_definitions USING btree (name, project_id) NULLS NOT DISTINCT
    WHERE (deleted_at IS NULL);
"""


@pytest.fixture(autouse=True)
def _reset_revision_globals() -> Iterator[None]:
    reset_definition_revision_state()
    yield
    reset_definition_revision_state()


@pytest.fixture
def definition_db(postgres_database_url: str) -> Iterator[PostgresHubDatabase]:
    schema = f"gobby_test_defmgr_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(_SCHEMA_SQL)
    database = PostgresHubDatabase(f"{postgres_database_url}?options=-csearch_path%3D{schema}")
    try:
        yield database
    finally:
        database.close()
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
