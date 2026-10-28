# Read the daemon database with query_readonly

Load before calling `gobby-sessions:query_readonly`, or when a `query_readonly`
refusal or failure needs diagnosing. Fetch the schema first with
`get_tool_schema(server_name="gobby-sessions", tool_name="query_readonly")`.

Only the Assistant and the Orchestrator may call it. Every other seat is refused
by the `query-readonly-seat-scope` rule. Route the database need to Triage or
your Dev Manager through `gobby-agents:send_message`.

## Call shape

- `sql`: one `SELECT` or `WITH` statement. A single trailing `;` is stripped.
  Any other `;` is refused with "Send one statement per call."
- `params`: bind values with `%s` placeholders. Never splice values into `sql`.
- `limit`: integer, default 200, clamped to 1 through 2000. A boolean or
  non-integer returns `invalid_query`.

The daemon wraps the statement as `SELECT * FROM (...) AS readonly_result LIMIT n`,
so an inner `LIMIT` cannot raise the row cap.

## Execution limits

- The statement runs in a read-only, repeatable-read transaction.
- A 5-second statement timeout applies.
- It runs as the `gobby_query_readonly` role (migration 482). PostgreSQL privilege
  checks apply to that role, not to the daemon runtime role.

## Refusals

Refusals return `success: false` with `error_code: "query_refused"`. The message
names the cause. Refusals cover these cases:

- Non-ASCII text, `U&` escape syntax, or a statement that does not start with
  `SELECT` or `WITH`.
- A credential table: `secrets`, `secret_key_material`, `api_keys`, or
  `auth_sessions`.
- A column matching a credential pattern: `%secret%`, `%encrypted%`,
  `%wrapped_dek%`, `%kek_%`, `%key_hash%`, `%token_hash%`, or `%password%`.
- A function defined outside `pg_catalog`.
- A file, signal, or text-SQL function: names with the prefixes `dblink`, `lo_`,
  `pg_ls_`, `pg_read_`, `pg_terminate_`, `pg_cancel_`, `pg_reload_`, or
  `pg_rotate_`; any name containing `to_xml`; and `set_config`, `ts_rewrite`,
  `ts_stat`, and the other XML-from-query functions.

## Results

- Success: `{"success": true, "rows": [...], "limit": N}`. Values that JSON cannot
  encode, such as timestamps and UUIDs, come back as strings.
- Database error: `success: false` with `error_code: "query_failed"`. A timeout
  reports here.
- Bad `limit`: `success: false` with `error_code: "invalid_query"`.

## Handling results

Do not print credential material, even when a query returns it. Summarize the
rows the task needs. Keep the query narrow: select named columns, filter in SQL,
and rely on `limit` as the backstop.

_Last verified: 2026-10-27_
