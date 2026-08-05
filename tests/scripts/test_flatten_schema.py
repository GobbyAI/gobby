from __future__ import annotations

import hashlib
import json

import pytest

from gobby.storage.migration_flatten import MigrationReceipt
from scripts.flatten_schema import build_evidence_manifest, render_baseline

pytestmark = pytest.mark.unit


def test_render_baseline_is_stable_and_preserves_cluster_auth() -> None:
    schema_dump = r"""
-- PostgreSQL database dump
\restrict random-token
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);
CREATE EXTENSION IF NOT EXISTS pg_search WITH SCHEMA public;
COMMENT ON EXTENSION pg_search IS 'search';
CREATE SCHEMA public;
COMMENT ON SCHEMA public IS 'standard public schema';
CREATE TABLE public.tasks (id uuid PRIMARY KEY, title text NOT NULL);
CREATE SCHEMA gobby_agent_auth;
CREATE FUNCTION gobby_agent_auth.issue() RETURNS void LANGUAGE sql AS $$ SELECT $$;
GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue() TO gobby_agent_managed_issuer;
\unrestrict random-token
"""
    seed_dump = r"""
-- PostgreSQL database dump
\restrict another-random-token
INSERT INTO public.projects (id, name) VALUES ('zero', '_orphaned');
\unrestrict another-random-token
"""

    first = render_baseline(schema_dump, seed_dump, application_schema="public")
    second = render_baseline(schema_dump, seed_dump, application_schema="public")

    assert first == second
    assert "random-token" not in first
    assert "CREATE EXTENSION" not in first
    assert "CREATE SCHEMA public" not in first
    assert "public.tasks" not in first
    assert "CREATE TABLE tasks" in first
    assert "INSERT INTO projects" in first
    assert "CREATE SCHEMA gobby_agent_auth" in first
    assert "GRANT EXECUTE ON FUNCTION gobby_agent_auth.issue()" in first


def test_render_baseline_uses_current_user_for_migration_owner_policies() -> None:
    schema_dump = """
CREATE TABLE public.projects (id uuid PRIMARY KEY);
CREATE POLICY gobby_migration_owner_access
ON public.projects TO gobby USING (true) WITH CHECK (true);
ALTER DEFAULT PRIVILEGES FOR ROLE gobby IN SCHEMA public
GRANT SELECT ON TABLES TO gobby_daemon_runtime;
"""

    baseline = render_baseline(schema_dump, "", application_schema="public")

    assert "TO CURRENT_USER USING (true)" in baseline
    assert "TO gobby USING (true)" not in baseline
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public" in baseline


def test_evidence_manifest_pins_ddl_seed_and_receipts() -> None:
    ddl = "CREATE TABLE tasks (id uuid);\n"
    seeds = '{"projects": []}\n'
    receipts = (
        MigrationReceipt(354, "354_bookkeeping.sql", "a" * 64),
        MigrationReceipt(355, "355_reconcile.sql", "b" * 64),
    )

    manifest = json.loads(
        build_evidence_manifest(
            baseline_version=375,
            baseline_sql="CREATE TABLE schema_migrations (version integer);\n",
            normalized_ddl=ddl,
            seed_manifest=seeds,
            divergence_ledger="# ledger\n",
            applied_versions=(305, 354, 355, 375),
            receipts=receipts,
        )
    )

    assert manifest["baseline_version"] == 375
    assert manifest["normalized_ddl"]["sha256"] == hashlib.sha256(ddl.encode()).hexdigest()
    assert manifest["seed_manifest"]["sha256"] == hashlib.sha256(seeds.encode()).hexdigest()
    assert manifest["applied_versions"] == [305, 354, 355, 375]
    assert manifest["receipts"][0] == {
        "checksum": "a" * 64,
        "filename": "354_bookkeeping.sql",
        "version": 354,
    }
