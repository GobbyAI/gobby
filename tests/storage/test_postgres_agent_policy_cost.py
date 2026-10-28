"""Row-level-security cost guards for scoped managed-agent roles (#25294).

``gobby_agent_auth.current_read_code_project_ids()`` is ``STABLE``, so PostgreSQL re-runs it for
every row a sequential scan filters through ``project_id = ANY (current_read_code_project_ids())``.
Each call looks up the principal binding and scans ``worktrees`` and ``clones``, so one agent
``code_symbols`` read over a few hundred thousand rows produced billions of ``worktrees`` scans and
saturated the daemon's Postgres across parallel workers. A policy helper must run a bounded number
of times per query, never once per row.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from tests.storage.test_postgres_agent_authorization import (
    AuthorizationFixture,
    _authorization_fixture,
)

pytestmark = pytest.mark.integration

OUT_OF_SCOPE_ROWS = 3_000
IN_SCOPE_ROWS = 5
POLICY_CALLS_PER_QUERY_CEILING = 10

POLICY_CALLS = """
    SELECT COALESCE(SUM(calls), 0)
    FROM pg_stat_xact_user_functions
    WHERE schemaname = 'gobby_agent_auth'
"""

# Both tables carry the `project_id = ANY (current_read_code_project_ids())` read policy and
# reference the one `code_indexed_files` row the authorization fixture seeds per project.
SEED_SYMBOLS = """
    INSERT INTO public.code_symbols (
        id, project_id, file_path, name, qualified_name, kind, language,
        byte_start, byte_end, line_start, line_end, content_hash, file_content_hash
    )
    SELECT gen_random_uuid(), %(project)s, 'src/lib.rs', 'bulk_' || g, 'crate::bulk_' || g,
           'function', 'rust', 0, 10, 1, 1, 'bulk-' || g, 'hash-' || %(project)s
    FROM generate_series(1, %(rows)s) g
"""
SEED_FILES = """
    INSERT INTO public.code_indexed_files
        (id, project_id, file_path, language, content_hash, symbol_count, byte_size)
    SELECT gen_random_uuid(), %(project)s, 'bulk/' || g, 'rust', 'bulk-' || g, 1, 10
    FROM generate_series(1, %(rows)s) g
"""
SCANS = {
    "code_symbols": "SELECT count(*) FROM code_symbols WHERE name LIKE 'bulk_%'",
    "code_indexed_files": "SELECT count(*) FROM code_indexed_files WHERE file_path LIKE 'bulk/%'",
}


@pytest.fixture
def populated_index(postgres_database_url: str) -> Iterator[AuthorizationFixture]:
    """A scoped agent whose project owns a handful of rows among thousands it cannot read."""
    with _authorization_fixture(postgres_database_url) as fixture:
        with psycopg.connect(fixture.database_url, autocommit=True) as admin:
            for project_id, rows in (
                (fixture.other_project_id, OUT_OF_SCOPE_ROWS),
                (fixture.project_id, IN_SCOPE_ROWS),
            ):
                for seed in (SEED_FILES, SEED_SYMBOLS):
                    admin.execute(seed, {"project": project_id, "rows": rows})
            admin.execute("ANALYZE public.code_symbols")
            admin.execute("ANALYZE public.code_indexed_files")
            # Track the policy helpers inside the agent's own sessions; only a superuser may
            # enable this, and ALTER ROLE applies it to the agent's next login.
            admin.execute(
                sql.SQL("ALTER ROLE {} SET track_functions = 'all'").format(
                    sql.Identifier(fixture.role_name)
                )
            )
        yield fixture


@pytest.mark.parametrize("table", sorted(SCANS))
def test_scoped_scan_runs_policy_helpers_a_bounded_number_of_times(
    populated_index: AuthorizationFixture, table: str
) -> None:
    """A full-table scan as an agent calls the policy helpers O(1) times, not once per row."""
    with psycopg.connect(populated_index.agent_url) as agent:
        # Force the sequential-scan shape production hit, and keep every call in this backend
        # so the transaction-local function statistics see all of them.
        agent.execute("SET LOCAL enable_indexscan = off")
        agent.execute("SET LOCAL enable_bitmapscan = off")
        agent.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        visible = agent.execute(SCANS[table]).fetchone()
        calls = agent.execute(POLICY_CALLS).fetchone()
    # Row-level security still confines the agent to its own project.
    assert visible == (IN_SCOPE_ROWS,)
    assert calls is not None
    assert calls[0] <= POLICY_CALLS_PER_QUERY_CEILING, (
        f"{calls[0]} policy helper calls for one scan of {OUT_OF_SCOPE_ROWS + IN_SCOPE_ROWS} "
        f"{table} rows"
    )
