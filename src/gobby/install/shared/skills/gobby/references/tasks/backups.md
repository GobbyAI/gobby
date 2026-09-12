# Backups, import, and maintenance

Load for task backup/restore, external issue import, or search/data maintenance.
Discover `backup_tasks` and `restore_tasks` on `gobby-tasks`; discover
`import_github_issues`, `link_task_to_github_issue`, and `reindex_tasks` on
`gobby-tasks-ops`. Use tool schemas for repository, path, and import filters.

PostgreSQL is authoritative. The default project backup is
`~/.gobby/backups/<project-uuid>/tasks.jsonl`; backups contain current live rows.
`backup_tasks` can select an output path. `restore_tasks` upserts stable IDs only
when backup timestamps are newer, preserves database-only/newer rows, and is
explicit rather than a startup action. Inspect the project and source backup,
take a current backup when appropriate, then check reported counts. Test restore
examples only against isolated fixtures or temporary daemon state.

Import GitHub issues through the import tool, then inspect resulting tasks and
links. Linking an existing task to an issue changes provenance; it is not a PR
delivery or issue-close operation. External delivery belongs to source-control.

The following task CLI procedures are operator-only: `compact analyze`,
`compact apply`, `compact stats`, `doctor`, `clean`, `repair-lifecycle`, and
`validation-history --clear`. Compaction can replace task detail with a summary;
repair and cleanup can alter historical state. Inspect their current CLI options
and dry-run/preview behavior where provided before executing an authorized
operation. Agents do not use these to evade MCP lifecycle gates.

Search reindexing rebuilds the task search index, not task records. For malformed
backups, missing checkouts, or import authentication errors, retain the error and
repair the source/configuration; do not manufacture task state or use a different
project to make an operation succeed. Hub-wide disaster recovery belongs to admin.

Guides: [Storage and Backups](../../../../../../../../docs/guides/tasks.md#storage-and-backups),
[Task CLI](../../../../../../../../docs/guides/cli-commands.md#task-lifecycle).

_Last verified: 2026-09-12_
