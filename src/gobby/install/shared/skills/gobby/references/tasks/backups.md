# Backups, import, and maintenance

Load for task backup/restore or search/data maintenance.
Discover `backup_tasks` on `gobby-tasks` and `reindex_tasks` on
`gobby-tasks-ops`. Use tool schemas for path filters.

PostgreSQL is authoritative. `backup_tasks` writes current live rows to the
project backup `~/.gobby/backups/<project-uuid>/tasks.jsonl` and takes no path.
Restore has no MCP tool: the operator runs `gobby tasks restore` in their own
shell, and agent shells are refused. It upserts stable IDs only when backup
timestamps are newer, preserves database-only/newer rows, and is explicit rather
than a startup action. When a restore is needed, inspect the project and source
backup, take a current backup when appropriate, relay the exact restore command
to the operator, then check reported counts. Test restore examples only against
isolated fixtures or temporary daemon state.

The following task CLI procedures are operator-only: `restore`, `compact analyze`,
`compact apply`, `compact stats`, `doctor`, `clean`, and
`validation-history --clear`. Compaction can replace task detail with a summary;
repair and cleanup can alter historical state. Inspect their current CLI options
and dry-run/preview behavior where provided before executing an authorized
operation. Agents do not use these to evade MCP lifecycle gates.

Search reindexing rebuilds the task search index, not task records. For malformed
backups or missing checkouts, retain the error and repair the source; do not
manufacture task state or use a different project to make an operation succeed.
Hub-wide disaster recovery belongs to admin.

Guides: [Storage and Backups](../../../../../../../../docs/guides/tasks.md#storage-and-backups),
[Task CLI](../../../../../../../../docs/guides/cli-commands.md#task-lifecycle).

_Last verified: 2026-10-22_
