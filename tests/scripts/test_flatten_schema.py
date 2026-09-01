"""Tests for canonical embedded-schema baseline generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import flatten_schema, verify_flatten

pytestmark = pytest.mark.unit


def test_render_dump_preserves_security_definer_schema_references() -> None:
    dump = """\
-- PostgreSQL database dump
CREATE TABLE public.machines (id uuid NOT NULL);
CREATE FUNCTION gobby_agent_auth.heartbeat_daemon() RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'gobby_agent_auth', 'pg_temp'
    AS $$
    -- Function-body comments are schema semantics for lossless dump comparison.
    SELECT id FROM public.machines
    $$;
"""

    rendered = flatten_schema.render_dump(dump, application_schema="public")

    assert "CREATE TABLE machines" in rendered
    assert "gobby_agent_auth.heartbeat_daemon" in rendered
    assert "-- Function-body comments" in rendered
    assert "FROM public.machines" in rendered


def test_canonicalize_seed_dump_sorts_rows_and_replaces_machine_timestamps() -> None:
    dump = """\
INSERT INTO public.config_state (id, revision) VALUES (true, 0);
INSERT INTO public.projects (id, name, created_at, updated_at)
VALUES ('b', 'Beta', '2026-01-02 00:00:00+00', '2026-01-02 00:00:00+00');
INSERT INTO public.projects (id, name, created_at, updated_at)
VALUES ('a', 'Alpha', '2026-01-01 00:00:00+00', '2026-01-01 00:00:00+00');
"""

    rendered = flatten_schema.canonicalize_seed_dump(dump)

    assert rendered.splitlines()[0] == "INSERT INTO config_state (id, revision) VALUES (true, 0);"
    assert "'a', 'Alpha', NOW(), NOW()" in rendered.splitlines()[2]
    assert "'b', 'Beta', NOW(), NOW()" in rendered.splitlines()[4]


def test_assemble_baseline_preserves_pgcrypto_substrate() -> None:
    source = """\
SELECT pg_advisory_xact_lock(1);
SET check_function_bodies = false;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA "public";
GRANT EXECUTE ON FUNCTION "public".digest(BYTEA, TEXT) TO gobby_agent_issuer;
DO $migration$
BEGIN
    EXECUTE 'CREATE FUNCTION public.gobby_maintenance_epoch_login_guard() RETURNS event_trigger';
END;
$migration$;
DO $migration$
BEGIN
    EXECUTE 'CREATE EVENT TRIGGER gobby_maintenance_epoch_login_guard ON login';
END;
$migration$;
REVOKE ALL ON FUNCTION public.gobby_maintenance_epoch_login_guard() FROM PUBLIC;
GRANT ALL ON FUNCTION public.gobby_maintenance_epoch_login_guard() TO gobby_daemon_runtime;
"""

    baseline = flatten_schema.assemble_baseline(
        source_baseline=source,
        schema_dump="CREATE TABLE public.sample (id integer);",
        seed_sql="",
        version=420,
    )

    assert 'CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA "public";' in baseline
    assert (
        'GRANT EXECUTE ON FUNCTION "public".digest(BYTEA, TEXT) TO gobby_agent_issuer;' in baseline
    )
    assert baseline.index("CREATE TABLE sample") < baseline.index("CREATE EXTENSION")


def test_write_generated_pins_canonical_inputs(tmp_path: Path) -> None:
    baseline_path = tmp_path / "schema" / "baseline.sql"
    evidence_dir = tmp_path / "evidence"
    generated = flatten_schema.GeneratedBaseline(
        baseline_sql="-- canonical baseline\n",
        normalized_ddl="CREATE TABLE __schema__.sample (id integer);\n",
        seed_sql="INSERT INTO sample (id) VALUES (1);\n",
        source_identity={"baseline_version": 419},
    )

    manifest = flatten_schema.write_generated(
        generated,
        baseline_path=baseline_path,
        evidence_dir=evidence_dir,
        version=420,
    )

    assert baseline_path.read_text(encoding="utf-8") == generated.baseline_sql
    assert (evidence_dir / "canonical.normalized.sql").read_text(encoding="utf-8") == (
        generated.normalized_ddl
    )
    assert (evidence_dir / "canonical.seeds.sql").read_text(encoding="utf-8") == (
        generated.seed_sql
    )
    assert manifest["baseline_version"] == 420
    assert manifest["source_identity"] == {"baseline_version": 419}


def test_defaults_target_baseline_420_evidence() -> None:
    assert flatten_schema.DEFAULT_VERSION == 420
    assert flatten_schema.DEFAULT_OUTPUT_DIR == Path("docs/evidence/flatten-baseline-420")


def test_successful_verification_removes_a_stale_diff(tmp_path: Path) -> None:
    old = verify_flatten.Snapshot("old", {}, "old dump\n", [], [])
    changed = verify_flatten.Snapshot("new", {}, "new dump\n", [], [])
    identical = verify_flatten.Snapshot("new", {}, old.dump, [], [])

    assert verify_flatten.write_evidence(tmp_path, old, changed)
    assert (tmp_path / "dump.diff").exists()

    assert not verify_flatten.write_evidence(tmp_path, old, identical)
    assert not (tmp_path / "dump.diff").exists()
