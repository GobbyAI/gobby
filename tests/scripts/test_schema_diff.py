from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scripts.schema_diff import (
    SeedTableSpec,
    compare_live_seed_manifests,
    compare_machine_seed_manifests,
    normalize_schema_dump,
    normalize_seed_rows,
    seed_manifest_json,
)

pytestmark = pytest.mark.unit


def test_normalize_schema_dump_ignores_dump_noise_schema_name_and_column_order() -> None:
    fresh = r"""
-- PostgreSQL database dump
\restrict RANDOM_FRESH_TOKEN
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);
CREATE SCHEMA gobby_test_123;
CREATE TABLE gobby_test_123.widgets (
    id uuid NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    CONSTRAINT widgets_pkey PRIMARY KEY (id)
);
CREATE INDEX widgets_label_idx ON gobby_test_123.widgets USING btree (label);
\unrestrict RANDOM_FRESH_TOKEN
"""
    live = r"""
-- PostgreSQL database dump
\restrict RANDOM_LIVE_TOKEN
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);
CREATE SCHEMA public;
COMMENT ON SCHEMA public IS 'standard public schema';
CREATE TABLE public.widgets (
    label text DEFAULT ''::text NOT NULL,
    CONSTRAINT widgets_pkey PRIMARY KEY (id),
    id uuid NOT NULL
);
CREATE INDEX widgets_label_idx ON public.widgets USING btree (label);
\unrestrict RANDOM_LIVE_TOKEN
"""

    assert normalize_schema_dump(fresh, schema_name="gobby_test_123") == normalize_schema_dump(
        live,
        schema_name="public",
    )


def test_normalize_schema_dump_preserves_definition_drift() -> None:
    fresh = "CREATE TABLE scratch.items (id uuid NOT NULL, total bigint NOT NULL);"
    live = "CREATE TABLE public.items (id uuid NOT NULL, total integer NOT NULL);"

    assert normalize_schema_dump(fresh, schema_name="scratch") != normalize_schema_dump(
        live,
        schema_name="public",
    )


def test_normalize_schema_dump_removes_documented_accepted_table_objects() -> None:
    dump = """
CREATE TABLE public._pgaudit_probe (id integer NOT NULL);
CREATE SEQUENCE public._pgaudit_probe_id_seq START WITH 1;
ALTER SEQUENCE public._pgaudit_probe_id_seq OWNED BY public._pgaudit_probe.id;
ALTER TABLE ONLY public._pgaudit_probe ALTER COLUMN id
    SET DEFAULT nextval('public._pgaudit_probe_id_seq'::regclass);
ALTER TABLE ONLY public._pgaudit_probe
    ADD CONSTRAINT _pgaudit_probe_pkey PRIMARY KEY (id);
CREATE TABLE public.tasks (id uuid NOT NULL);
"""

    normalized = normalize_schema_dump(
        dump,
        schema_name="public",
        accepted_tables=frozenset({"_pgaudit_probe"}),
    )

    assert "_pgaudit_probe" not in normalized
    assert "CREATE TABLE __schema__.tasks" in normalized


def test_normalize_schema_dump_normalizes_embedded_current_schema_literal() -> None:
    fresh = """
CREATE FUNCTION scratch.guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_catalog.current_schema() IS DISTINCT FROM 'scratch' THEN
        RETURN NEW;
    END IF;
    RETURN NEW;
END;
$$;
"""
    live = fresh.replace("scratch", "public")

    assert normalize_schema_dump(fresh, schema_name="scratch") == normalize_schema_dump(
        live,
        schema_name="public",
    )


def test_machine_seed_comparison_is_exact_after_nondeterministic_field_normalization() -> None:
    spec = SeedTableSpec(
        key_columns=("id",),
        machine_mutable_columns=frozenset({"created_at", "updated_at"}),
    )
    expected = {
        "projects": normalize_seed_rows(
            table="projects",
            columns=("id", "name", "created_at", "updated_at"),
            rows=[
                (
                    "00000000-0000-0000-0000-000000000002",
                    "_global",
                    datetime(2026, 7, 31, 12, tzinfo=UTC),
                    datetime(2026, 7, 31, 12, tzinfo=UTC),
                )
            ],
            spec=spec,
        )
    }
    actual = {
        "projects": normalize_seed_rows(
            table="projects",
            columns=("id", "name", "created_at", "updated_at"),
            rows=[
                (
                    "00000000-0000-0000-0000-000000000002",
                    "_global",
                    datetime(2026, 8, 1, 9, tzinfo=UTC),
                    datetime(2026, 8, 1, 9, tzinfo=UTC),
                )
            ],
            spec=spec,
        )
    }

    assert compare_machine_seed_manifests(expected, actual) == ()


def test_machine_seed_comparison_reports_immutable_definition_drift() -> None:
    spec = SeedTableSpec(key_columns=("name",))
    expected = {
        "registry": normalize_seed_rows(
            table="registry",
            columns=("name", "definition"),
            rows=[("development", {"review": "required"})],
            spec=spec,
        )
    }
    actual = {
        "registry": normalize_seed_rows(
            table="registry",
            columns=("name", "definition"),
            rows=[("development", {"review": "none"})],
            spec=spec,
        )
    }

    assert compare_machine_seed_manifests(expected, actual) == (
        "registry seed row ('development',) differs",
    )


def test_live_seed_comparison_checks_required_rows_and_owned_namespace() -> None:
    spec = SeedTableSpec(
        key_columns=("name",),
        live_mutable_columns=frozenset({"enabled", "definition", "updated_at"}),
        live_namespace_owned=True,
    )
    expected = {
        "registry": normalize_seed_rows(
            table="registry",
            columns=("name", "enabled", "definition", "updated_at"),
            rows=[("development", True, "bundled", datetime(2026, 7, 31, tzinfo=UTC))],
            spec=spec,
        )
    }
    live = {
        "registry": normalize_seed_rows(
            table="registry",
            columns=("name", "enabled", "definition", "updated_at"),
            rows=[
                ("development", False, "refreshed", datetime(2026, 8, 1, tzinfo=UTC)),
                ("unexpected", True, "foreign", datetime(2026, 8, 1, tzinfo=UTC)),
            ],
            spec=spec,
        )
    }

    assert compare_live_seed_manifests(expected, live, {"registry": spec}) == (
        "registry has unexpected seed-owned key ('unexpected',)",
    )


def test_live_seed_comparison_allows_extra_rows_in_mixed_application_table() -> None:
    spec = SeedTableSpec(key_columns=("id",), live_namespace_owned=False)
    expected = {
        "projects": normalize_seed_rows(
            table="projects",
            columns=("id", "name"),
            rows=[("reserved", "_global")],
            spec=spec,
        )
    }
    live = {
        "projects": normalize_seed_rows(
            table="projects",
            columns=("id", "name"),
            rows=[("reserved", "_global"), ("user-project", "gobby")],
            spec=spec,
        )
    }

    assert compare_live_seed_manifests(expected, live, {"projects": spec}) == ()


def test_seed_manifest_json_is_canonical() -> None:
    spec = SeedTableSpec(key_columns=("id",))
    manifest = {
        "projects": normalize_seed_rows(
            table="projects",
            columns=("id", "metadata"),
            rows=[("b", {"z": 1, "a": [2, 1]}), ("a", None)],
            spec=spec,
        )
    }

    rendered = seed_manifest_json(manifest)
    payload = json.loads(rendered)

    assert rendered.endswith("\n")
    assert [row["key"] for row in payload["projects"]] == [["a"], ["b"]]
    assert rendered.index('"a": [') < rendered.index('"z": 1')
