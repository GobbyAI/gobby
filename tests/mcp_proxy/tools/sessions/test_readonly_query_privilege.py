"""Credential sentinels must stay unreadable through query_readonly by any path.

Each test seeds a synthetic credential sentinel in a temporary schema on the
isolated test hub, then asks ``query_readonly`` for it through a different
catalog or function path. The assertion is only that the sentinel never
appears in a returned row. A refusal or a permission error both pass.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.sessions._readonly_query import SQL_FROM_TEXT_FUNCTIONS
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("query_readonly_gdaemon")]

TOOL = "query_readonly"
SENTINEL = "credential-sentinel-25195-value"
ACTIVITY_SENTINEL = "credential-sentinel-25195-activity"
ROW_COUNT = 200
SENTINEL_ROWS = 150


@pytest.fixture
def registry(temp_db: HubDatabase) -> InternalToolRegistry:
    return create_session_messages_registry(session_manager=MagicMock(), db=temp_db)


@pytest.fixture
def fixture_schema(temp_db: HubDatabase) -> Iterator[str]:
    """Synthetic credential table with repeated sentinel values, analyzed for stats."""
    schema = f"sentinel_{uuid.uuid4().hex[:12]}"
    with temp_db.transaction() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.secrets (id int, encrypted_value text)")
        conn.execute(
            f"INSERT INTO {schema}.secrets "
            "SELECT g, CASE WHEN g <= %s THEN %s ELSE 'filler-' || g END "
            f"FROM generate_series(1, {ROW_COUNT}) g",
            (SENTINEL_ROWS, SENTINEL),
        )
        conn.execute(f"ANALYZE {schema}.secrets")
        conn.execute(
            f"CREATE VIEW {schema}.plain AS SELECT encrypted_value AS payload FROM {schema}.secrets"
        )
    try:
        yield schema
    finally:
        with temp_db.transaction() as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


async def _run(registry: InternalToolRegistry, **arguments: Any) -> dict[str, Any]:
    result: dict[str, Any] = await registry.call(TOOL, arguments)
    return result


def _leaked(result: dict[str, Any], needle: str) -> bool:
    return needle in json.dumps(result.get("rows", []), default=str)


async def test_pg_stats_most_common_values_hide_the_sentinel(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(
        registry,
        sql=(
            "SELECT most_common_vals::text AS v FROM pg_stats "
            f"WHERE tablename = concat('sec','rets') AND schemaname = '{fixture_schema}'"
        ),
    )

    assert not _leaked(result, SENTINEL)


async def test_pg_statistic_does_not_expose_the_sentinel(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(registry, sql="SELECT stavalues1::text AS v FROM pg_statistic")

    assert not _leaked(result, SENTINEL)


async def test_pg_stat_activity_hides_other_session_text(
    registry: InternalToolRegistry, postgres_database_url: str
) -> None:
    other = psycopg.connect(postgres_database_url, autocommit=True)
    try:
        other.execute(f"SELECT '{ACTIVITY_SENTINEL}'::text")

        result = await _run(registry, sql="SELECT query AS v FROM pg_stat_activity")
    finally:
        other.close()

    assert not _leaked(result, ACTIVITY_SENTINEL)


async def test_neutral_view_over_credentials_is_unreadable(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(registry, sql=f"SELECT payload::text AS v FROM {fixture_schema}.plain")

    assert not _leaked(result, SENTINEL)


async def test_set_config_cannot_reset_role_to_read_credentials(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(
        registry,
        sql=(
            "SELECT (SELECT set_config('role', 'none', true)) AS a, "
            f"(SELECT string_agg(payload, ',') FROM {fixture_schema}.plain) AS v"
        ),
    )

    assert not _leaked(result, SENTINEL)


async def test_query_to_xml_cannot_read_credentials(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(
        registry,
        sql=(
            "SELECT query_to_xml("
            f"'SELECT string_agg(payload, '','') AS v FROM {fixture_schema}.plain', "
            "false, false, '')::text AS v"
        ),
    )

    assert not _leaked(result, SENTINEL)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT set_config('role', 'none', true) AS a",
        "SELECT pg_catalog.set_config('role', 'none', true) AS a",
        "SELECT \"set_config\"('role', 'none', true) AS a",
    ],
)
async def test_set_config_is_refused_by_name(registry: InternalToolRegistry, sql: str) -> None:
    result = await _run(registry, sql=sql)

    assert result["success"] is False


async def test_statement_runs_as_the_query_role(registry: InternalToolRegistry) -> None:
    result = await _run(registry, sql="SELECT current_user::text AS v")

    assert result["success"] is True
    assert result["rows"] == [{"v": "gobby_query_readonly"}]


async def test_mcp_server_env_is_unreadable(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    """mcp_servers has no credential-named column, so only the column grant hides env.

    The name read is a positive control: it must succeed, so a schema built without the
    query-role grants fails here instead of passing the env check vacuously.
    """
    with temp_db.transaction() as conn:
        project = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
    assert project is not None
    server_id = str(uuid.uuid4())
    server_name = f"sentinel-{server_id[:8]}"
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_servers (id, name, project_id, transport, env) "
            "VALUES (%s, %s, %s, 'stdio', %s)",
            (server_id, server_name, project["id"], json.dumps({"TOKEN": SENTINEL})),
        )
    try:
        control = await _run(registry, sql="SELECT name::text AS v FROM mcp_servers")
        result = await _run(registry, sql="SELECT env::text AS v FROM mcp_servers")
    finally:
        with temp_db.transaction() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = %s", (server_id,))

    assert control["success"] is True
    assert server_name in json.dumps(control["rows"], default=str)
    assert result["success"] is False
    assert "permission denied" in result["error"]
    assert not _leaked(result, SENTINEL)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT url::text AS v FROM mcp_servers",
        "SELECT args::text AS v FROM mcp_servers",
        "SELECT remote_url::text AS v FROM clones",
        "SELECT platform_url::text AS v FROM comms_attachments",
    ],
)
async def test_connection_url_and_args_columns_are_unreadable(
    registry: InternalToolRegistry, sql: str
) -> None:
    """URLs and args can carry tokens, so these columns carry no grant for the query role."""
    result = await _run(registry, sql=sql)

    assert result["success"] is False
    assert "permission denied" in result["error"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id::text AS v FROM mcp_servers",
        "SELECT id::text AS v FROM clones",
        "SELECT id::text AS v FROM comms_attachments",
    ],
)
async def test_same_tables_stay_readable_outside_the_denied_columns(
    registry: InternalToolRegistry, sql: str
) -> None:
    """Positive control: the denial is column-level, so the id column still reads."""
    result = await _run(registry, sql=sql)

    assert result["success"] is True


async def test_mcp_server_url_sentinel_stays_unreadable_direct_and_whole_row(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    """A token in mcp_servers.url is credential material even though the name looks benign."""
    with temp_db.transaction() as conn:
        project = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
    assert project is not None
    server_id = str(uuid.uuid4())
    server_name = f"sentinel-{server_id[:8]}"
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_servers (id, name, project_id, transport, url) "
            "VALUES (%s, %s, %s, 'stdio', %s)",
            (server_id, server_name, project["id"], f"https://example.test/mcp?token={SENTINEL}"),
        )
    try:
        control = await _run(registry, sql="SELECT name::text AS v FROM mcp_servers")
        direct = await _run(registry, sql="SELECT url::text AS v FROM mcp_servers")
        whole_row = await _run(registry, sql="SELECT to_jsonb(m)::text AS v FROM mcp_servers m")
    finally:
        with temp_db.transaction() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = %s", (server_id,))

    assert control["success"] is True
    assert direct["success"] is False
    assert whole_row["success"] is False
    assert not _leaked(direct, SENTINEL)
    assert not _leaked(whole_row, SENTINEL)


async def test_ts_stat_is_refused_by_name(registry: InternalToolRegistry) -> None:
    """ts_stat executes SQL from text, so it is refused by name like set_config.

    The inner text reads a column the role can read, so privileges cannot refuse it.
    Only the denied-name gate can, which makes this test fail if the name is removed.
    """
    result = await _run(
        registry,
        sql=("SELECT word::text AS v FROM ts_stat('SELECT to_tsvector(name) FROM mcp_servers')"),
    )

    assert result["success"] is False


# Real call shapes where a valid call proves the refusal; the rest are refused by name alone.
SQL_FROM_TEXT_SHAPES = {
    "dblink": "SELECT dblink('dbname=unused', 'SELECT 1')::text AS v",
    "query_to_xml": "SELECT query_to_xml('SELECT 1', false, false, '')::text AS v",
    "query_to_xml_and_xmlschema": (
        "SELECT query_to_xml_and_xmlschema('SELECT 1', false, false, '')::text AS v"
    ),
    "query_to_xmlschema": "SELECT query_to_xmlschema('SELECT 1', false, false, '')::text AS v",
    "set_config": "SELECT set_config('role', 'none', true)::text AS v",
    "ts_rewrite": (
        "SELECT ts_rewrite('a'::tsquery, 'SELECT ''a''::tsquery, ''b''::tsquery')::text AS v"
    ),
    "ts_stat": "SELECT word::text AS v FROM ts_stat('SELECT ''x''::tsvector')",
}


@pytest.mark.parametrize("name", sorted(SQL_FROM_TEXT_FUNCTIONS))
async def test_sql_from_text_builtin_is_refused_by_name(
    registry: InternalToolRegistry, name: str
) -> None:
    """Each built-in that runs SQL held in text is refused before Postgres resolves the call."""
    sql = SQL_FROM_TEXT_SHAPES.get(name, f"SELECT {name}()::text AS v")
    result = await _run(registry, sql=sql)

    assert result["success"] is False
    assert result["error_code"] == "query_refused"


async def test_function_outside_pg_catalog_is_refused(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    """A public plpgsql function can EXECUTE text, so any function outside pg_catalog is refused.

    The query role holds USAGE on the schema, so only the name gate can refuse the call.
    """
    schema = f"fn_{uuid.uuid4().hex[:12]}"
    with temp_db.transaction() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(
            f"CREATE FUNCTION {schema}.run_text(sql text) RETURNS text LANGUAGE plpgsql AS "
            "$$ BEGIN EXECUTE sql; RETURN 'ran'; END $$"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO gobby_query_readonly")
        conn.execute(f"GRANT EXECUTE ON FUNCTION {schema}.run_text(text) TO gobby_query_readonly")
    try:
        result = await _run(registry, sql=f"SELECT {schema}.run_text('SELECT 1')::text AS v")
    finally:
        with temp_db.transaction() as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")

    assert result["success"] is False
    assert result["error_code"] == "query_refused"


async def test_validation_argv_is_withheld_from_the_query_role(
    registry: InternalToolRegistry,
) -> None:
    """validation_run_records.argv can carry command tokens, so the query role gets no SELECT.

    The id read is a positive control: the denial is column-level, not table-level.
    """
    privilege = await _run(
        registry,
        sql=(
            "SELECT has_column_privilege('gobby_query_readonly', "
            "'validation_run_records', 'argv', 'SELECT')::text AS v"
        ),
    )
    control = await _run(registry, sql="SELECT id::text AS v FROM validation_run_records")
    denied = await _run(registry, sql="SELECT argv::text AS v FROM validation_run_records")

    assert privilege["success"] is True
    assert privilege["rows"] == [{"v": "false"}]
    assert control["success"] is True
    assert denied["success"] is False
    assert "permission denied" in denied["error"]


# Query 1 of the reporter procedure, verbatim. EXISTS and NOT EXISTS share names with pdb
# functions, so the name gate must leave them alone.
QUERY_ONE_SQL = """\
WITH in_flight AS (
  SELECT t.id, t.seq_num, t.title, t.claimed_by_session_id
  FROM tasks t
  WHERE t.project_id = %s AND t.closed_at IS NULL
    AND t.path_cache ~ %s AND t.seq_num > %s
    AND (
      t.claimed_by_session_id IS NOT NULL
      OR EXISTS (
        SELECT 1 FROM task_comments c
        WHERE c.task_id = t.id AND c.author_type = 'task_attestation'
      )
    )
),
active AS (
  SELECT c.task_id, c.created_at, c.body::jsonb AS evidence
  FROM task_comments c
  JOIN in_flight f ON f.id = c.task_id
  WHERE c.author_type = 'task_attestation'
    AND NOT EXISTS (
      SELECT 1 FROM task_comments correction
      WHERE correction.task_id = c.task_id
        AND correction.author_type = 'task_attestation'
        AND correction.body::jsonb ->> 'supersedes_id' = c.body::jsonb ->> 'id'
    )
),
latest AS (
  SELECT DISTINCT ON (task_id) task_id, created_at, evidence
  FROM active
  ORDER BY task_id, (evidence ->> 'sequence')::bigint DESC
),
claims AS (
  SELECT ci.task_id, min(ci.claimed_at) AS first_claimed_at
  FROM task_claim_intervals ci
  JOIN in_flight f ON f.id = ci.task_id
  GROUP BY ci.task_id
)
SELECT f.seq_num, f.title, f.claimed_by_session_id,
       l.evidence ->> 'kind' AS evidence_kind,
       l.evidence ->> 'result' AS evidence_result,
       l.evidence ->> 'summary' AS evidence_summary,
       l.evidence -> 'reference' AS evidence_reference,
       l.created_at AS evidence_recorded_at,
       cl.first_claimed_at, count(*) OVER () AS total
FROM in_flight f
LEFT JOIN latest l ON l.task_id = f.id
LEFT JOIN claims cl ON cl.task_id = f.id
ORDER BY f.seq_num
"""

QUERY_ONE_ROOT = "9001"


def _seed_attested_task(temp_db: HubDatabase, project_id: str, title: str) -> str:
    """Create an open task under QUERY_ONE_ROOT with one implementation attestation."""
    task_id = (
        LocalTaskManager(temp_db)
        .create_task(project_id, title, validation_criteria="Rows return.")
        .id
    )
    temp_db.execute(
        "UPDATE tasks SET path_cache = %s WHERE id = %s", (f"{QUERY_ONE_ROOT}.1", task_id)
    )
    temp_db.execute(
        "INSERT INTO task_comments (id, task_id, author, author_type, body) "
        "VALUES (%s, %s, 'developer', 'task_attestation', %s)",
        (
            str(uuid.uuid4()),
            task_id,
            json.dumps(
                {
                    "id": str(uuid.uuid4()),
                    "kind": "implementation",
                    "result": "approved",
                    "summary": "Fixture evidence.",
                    "sequence": 1,
                    "reference": {"scheme": "git", "value": "a" * 40},
                }
            ),
        ),
    )
    return task_id


async def test_query_one_runs_verbatim_and_returns_the_attested_task(
    registry: InternalToolRegistry, temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    """Query 1 runs unchanged through query_readonly and returns the in-flight task row."""
    _seed_attested_task(temp_db, sample_project["id"], "Query one fixture")
    result = await _run(
        registry,
        sql=QUERY_ONE_SQL,
        params=[sample_project["id"], rf"^({QUERY_ONE_ROOT})\.", 0],
    )

    assert result["success"] is True, result.get("error")
    assert [row["title"] for row in result["rows"]] == ["Query one fixture"]
    assert result["rows"][0]["evidence_kind"] == "implementation"


# Keyword and alias names. None is a call of a function, so none may be refused by name.
KEYWORD_AND_ALIAS_SHAPES = {
    "exists": "SELECT 1 AS v WHERE EXISTS (SELECT 1)",
    "not_exists": "SELECT 1 AS v WHERE NOT EXISTS (SELECT 1 WHERE false)",
    "all": "SELECT 1 AS v WHERE 1 = ALL (SELECT 1)",
    "score_alias": "SELECT 1 AS score",
    "term_alias": "SELECT 1 AS term",
    "score_and_term_aliases": "SELECT 2 AS score, 3 AS term",
}


@pytest.mark.parametrize("name", sorted(KEYWORD_AND_ALIAS_SHAPES))
async def test_keyword_and_alias_names_pass_the_name_gate(
    registry: InternalToolRegistry, name: str
) -> None:
    """A keyword or a non-call alias that shares a spelling with a pdb function runs."""
    result = await _run(registry, sql=KEYWORD_AND_ALIAS_SHAPES[name])

    assert result["success"] is True, result.get("error")
    assert len(result["rows"]) == 1


# Comments and literals hold no call, so text inside them runs, including $ and backslashes.
HARMLESS_TEXT_SHAPES = {
    "block_comment_after_alias": "SELECT 1 AS score /* ordinary comment */",
    "line_comment_after_alias": "SELECT 1 AS term -- ordinary comment",
    "exists_with_block_comment": "SELECT EXISTS (SELECT 1) AS v /* ordinary comment */",
    "dollar_in_literal": "SELECT '$' AS note, 1 AS score",
    "dollar_quote_names_a_call": "SELECT $$run_text('SELECT 1')$$ AS v",
    "e_string_escaped_quote_then_alias": r"SELECT E'it\'s' AS note, 1 AS score",
    "mixed_case_dollar_tag_literal": "SELECT $A$'$a$' $A$ AS v",
    "nested_comment_literal": "SELECT '' /* outer /* inner */ ' */ AS v",
    "nested_comment_names_a_call": "SELECT 1 /* outer /* run_text('x') */ still comment */ AS v",
    "e_string_continuation_literal": "SELECT E'x'\n'y\\'' AS v",
    "e_string_continuation_after_comment_literal": "SELECT E'x' -- c\n'y\\'' AS v",
}


@pytest.mark.parametrize("name", sorted(HARMLESS_TEXT_SHAPES))
@pytest.mark.usefixtures("run_text_schema")
async def test_comments_and_literals_pass_the_name_gate(
    registry: InternalToolRegistry, name: str
) -> None:
    """Text inside a comment or a literal is not a call, so it runs."""
    result = await _run(registry, sql=HARMLESS_TEXT_SHAPES[name])

    assert result["success"] is True, result.get("error")
    assert len(result["rows"]) == 1


@pytest.fixture
def run_text_schema(temp_db: HubDatabase) -> Iterator[str]:
    """A plpgsql function outside pg_catalog that EXECUTEs its text, granted to the query role."""
    schema = f"fn_{uuid.uuid4().hex[:12]}"
    with temp_db.transaction() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(
            f"CREATE FUNCTION {schema}.run_text(sql text) RETURNS text LANGUAGE plpgsql AS "
            "$$ BEGIN EXECUTE sql; RETURN 'ran'; END $$"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO gobby_query_readonly")
        conn.execute(f"GRANT EXECUTE ON FUNCTION {schema}.run_text(text) TO gobby_query_readonly")
    try:
        yield schema
    finally:
        with temp_db.transaction() as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


# Call forms of functions outside pg_catalog. Each must be refused before Postgres runs it.
CALL_FORMS = {
    "unqualified": "SELECT run_text('SELECT 1')::text AS v",
    "schema_qualified_space_before_paren": "SELECT {schema}.run_text ('SELECT 1')::text AS v",
    "schema_qualified_newline_before_paren": "SELECT {schema}.run_text\n('SELECT 1')::text AS v",
    "uppercase": "SELECT {schema}.RUN_TEXT('SELECT 1')::text AS v",
    "quoted_function": "SELECT {schema}.\"run_text\"('SELECT 1')::text AS v",
    "quoted_schema_and_function": 'SELECT "{schema}"."run_text"(\'SELECT 1\')::text AS v',
    "comment_before_paren": "SELECT {schema}.run_text /*x*/ ('SELECT 1')::text AS v",
    "unicode_escape": "SELECT U&\"run\\005ftext\"('SELECT 1')::text AS v",
    "extension_unqualified": "SELECT score(1)::text AS v",
    "extension_schema_qualified": "SELECT pdb.score(1)::text AS v",
    "extension_quoted_keyword_name": 'SELECT "all"(1)::text AS v',
    "extension_quoted_exists": 'SELECT "exists"(1)::text AS v',
    "extension_quoted_schema_and_name": 'SELECT "pdb"."all"(1)::text AS v',
    "unqualified_comment_before_paren": "SELECT run_text/**/('SELECT 1')::text AS v",
    "schema_block_comment_no_space": "SELECT {schema}.run_text/**/('SELECT 1')::text AS v",
    "schema_line_comment": "SELECT {schema}.run_text -- x\n('SELECT 1')::text AS v",
    "extension_comment_before_paren": "SELECT score/**/(1)::text AS v",
    "dollar_quote_hides_quote": "SELECT $$'$$ || {schema}.run_text('SELECT 1')::text AS v",
    "tagged_dollar_quote_hides_quote": "SELECT $a$'$a$ || run_text('SELECT 1')::text AS v",
    "mixed_case_dollar_tag_hides_quote": "SELECT $A$'$a$' $A$ || run_text('SELECT 1')::text AS v",
    "mixed_case_dollar_tag_qualified": "SELECT $A$'$a$' $A$ || {schema}.run_text('SELECT 1')::text AS v",
    "nested_comment_hides_call": "SELECT '' /* outer /* inner */ ' */ || run_text('SELECT 1')::text AS v",
    "nested_comment_qualified": "SELECT '' /* outer /* inner */ ' */ || {schema}.run_text('SELECT 1')::text AS v",
    "e_string_continuation_hides_call": "SELECT E'x'\n'y\\'' || run_text('SELECT 1')::text AS v",
    "e_string_continuation_qualified": "SELECT E'x'\n'y\\'' || {schema}.run_text('SELECT 1')::text AS v",
    "e_string_continuation_after_comment": "SELECT E'x' -- c\n'y\\'' || run_text('SELECT 1')::text AS v",
    "e_string_quote_before_call": r"SELECT E'\'' || {schema}.run_text('SELECT 1')::text AS v",
    "e_string_backslash_before_call": r"SELECT E'\\' || run_text('SELECT 1')::text AS v",
    "e_string_quote_before_extension": r"SELECT E'\'', pdb.score(1)::text AS v",
    "qualified_keyword_call": "SELECT pdb.all(1)::text AS v",
    "attribute_notation_plpgsql": "SELECT ('SELECT 1').run_text::text AS v",
    "attribute_notation_extension": "SELECT ('x').score::text AS v",
}


@pytest.mark.parametrize("form", sorted(CALL_FORMS))
async def test_call_of_a_function_outside_pg_catalog_is_refused_in_every_form(
    registry: InternalToolRegistry, run_text_schema: str, form: str
) -> None:
    """Every call form of a non-pg_catalog function is refused by the name gate."""
    result = await _run(registry, sql=CALL_FORMS[form].format(schema=run_text_schema))

    assert result["success"] is False
    assert result["error_code"] == "query_refused"


# A column reached through a dot whose name matches a function outside pg_catalog. Attribute
# notation can call that function, and the gate cannot tell the two apart statically.
DOTTED_COLLISION_SHAPES = {
    "table_alias_column": "SELECT t.score FROM (SELECT 1 AS score) t",
    "parenthesized_row_column": "SELECT (t).score FROM (SELECT 1 AS score) t",
}


@pytest.mark.parametrize("name", sorted(DOTTED_COLLISION_SHAPES))
async def test_dotted_name_matching_a_function_is_refused_with_alias_guidance(
    registry: InternalToolRegistry, name: str
) -> None:
    """A dotted name matching a function is refused, with guidance to rename the alias."""
    result = await _run(registry, sql=DOTTED_COLLISION_SHAPES[name])

    assert result["success"] is False
    assert result["error_code"] == "query_refused"
    assert "alias" in result["error"]
