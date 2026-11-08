"""Plan coverage reads task rows through the sanctioned read role, scoped to its project."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "crates/gcore/assets/schema/migrations/490_grant_plan_coverage_reads.sql"
ASSETS = REPO_ROOT / "crates/gcore/src/schema/assets.rs"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").split())


def test_plan_coverage_reads_enable_row_security_on_both_task_tables() -> None:
    sql = _sql()

    assert "ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;" in sql
    assert "ALTER TABLE public.task_dependencies ENABLE ROW LEVEL SECURITY;" in sql


def test_sanctioned_read_role_sees_only_its_bound_project() -> None:
    sql = _sql()

    assert (
        "CREATE POLICY gobby_gcode_project_read ON public.tasks FOR SELECT "
        "TO gobby_gcode_capability USING (project_id = gobby_agent_auth.current_project_id());"
    ) in sql
    assert (
        "CREATE POLICY gobby_gcode_project_read ON public.task_dependencies FOR SELECT "
        "TO gobby_gcode_capability USING (EXISTS (SELECT 1 FROM public.tasks AS t "
        "WHERE t.id = task_dependencies.task_id));"
    ) in sql


def test_migration_grants_only_select_on_the_two_coverage_tables() -> None:
    grants = re.findall(r"GRANT [^;]+;", _sql())

    assert grants == [
        "GRANT SELECT ON TABLE public.tasks, public.task_dependencies TO gobby_gcode_capability;"
    ]


def test_existing_non_owner_roles_keep_their_unrestricted_access() -> None:
    sql = _sql()

    assert re.search(
        r"CREATE POLICY gobby_daemon_runtime_access ON public\.tasks "
        r"AS PERMISSIVE FOR ALL TO gobby_daemon_runtime USING \(true\) WITH CHECK \(true\);",
        sql,
    )
    assert re.search(
        r"CREATE POLICY gobby_daemon_runtime_access ON public\.task_dependencies "
        r"AS PERMISSIVE FOR ALL TO gobby_daemon_runtime USING \(true\) WITH CHECK \(true\);",
        sql,
    )
    assert re.search(
        r"CREATE POLICY gobby_agent_issuer_access ON public\.tasks "
        r"FOR SELECT TO gobby_agent_issuer USING \(true\);",
        sql,
    )
    assert re.search(
        r"CREATE POLICY gobby_query_readonly_access ON public\.tasks "
        r"FOR SELECT TO gobby_query_readonly USING \(true\);",
        sql,
    )
    assert re.search(
        r"CREATE POLICY gobby_query_readonly_access ON public\.task_dependencies "
        r"FOR SELECT TO gobby_query_readonly USING \(true\);",
        sql,
    )


def test_migration_is_registered_as_the_next_embedded_asset() -> None:
    assets = ASSETS.read_text(encoding="utf-8")

    assert 'filename: "490_grant_plan_coverage_reads.sql"' in assets
    assert (
        'include_str!("../../assets/schema/migrations/490_grant_plan_coverage_reads.sql")' in assets
    )
