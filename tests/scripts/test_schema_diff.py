from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest

import scripts.schema_diff as schema_diff
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


def test_normalize_schema_dump_matches_accepted_table_names_exactly() -> None:
    dump = """
CREATE TABLE public.tasks (id uuid NOT NULL);
CREATE TABLE public.task_stage_states (id uuid NOT NULL);
"""

    normalized = normalize_schema_dump(
        dump,
        schema_name="public",
        accepted_tables=frozenset({"tasks"}),
    )

    assert "CREATE TABLE __schema__.tasks (" not in normalized
    assert "CREATE TABLE __schema__.task_stage_states (" in normalized


def test_postgres_clients_keep_credentials_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def capture_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", capture_run)
    monkeypatch.setattr(schema_diff, "_apply_reconcile_migration", lambda *_: None)
    database_url = "postgresql://schema_diff_user:super-secret-password@127.0.0.1:5432/gobby_test"

    schema_diff._dump_schema(database_url, "public", pg_dump="pg_dump")
    schema_diff._project_live_schema(
        database_url,
        "CREATE SCHEMA public;",
        live_schema="public",
        projection_schema="projected",
        psql="psql",
    )

    assert len(calls) == 2
    for argv, kwargs in calls:
        rendered_argv = " ".join(argv)
        assert database_url not in rendered_argv
        assert "schema_diff_user" not in rendered_argv
        assert "super-secret-password" not in rendered_argv
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["PGUSER"] == "schema_diff_user"
        assert env["PGPASSWORD"] == "super-secret-password"


def test_main_reports_postgres_client_timeout_as_clean_nonzero_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def timed_out(argv: list[str], **_: object) -> None:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=120)

    def run_with_timeout(_: object) -> int:
        schema_diff._run_postgres_client(["pg_dump"], action="pg_dump schema public")
        pytest.fail("subprocess timeout was not raised")

    monkeypatch.setattr(subprocess, "run", timed_out)
    monkeypatch.setattr(schema_diff, "run", run_with_timeout)
    monkeypatch.setattr(sys, "argv", ["schema_diff.py"])

    assert schema_diff.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "schema_diff: pg_dump schema public timed out after 120 seconds; "
        "check database connectivity and retry\n"
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
