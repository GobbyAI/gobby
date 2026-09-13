# External issue integrations

Load for GitHub/Linear setup, issue import or synchronization. Discover the
configured external MCP server and its live tools; GitHub task import instead
uses locally authenticated `gh`. Confirm the project, repository/team/project
binding and requested direction before changing local or remote records.

1. Inspect operator `gobby github status --json`, `gobby linear status --json`,
   or `/api/projects/{project_id}/integrations/status` for configuration,
   connector readiness, counts, retry timing and recent errors.
2. Agent GitHub operations live on `gobby-tasks-ops`: fetch schemas for
   `import_github_issues`, `link_task_to_github_issue`, and
   `close_linked_github_issue`. Import deduplicates; linking changes local
   linkage; closing comments/labels/closes the external issue and requires the
   authorized merge workflow. PR delivery belongs to source-control.
   MCP re-import refreshes local title/body, labels and validation criteria;
   unlike CLI import it does not reconcile remote open/closed state.
3. Operator GitHub `link` supplies a repository default; `setup` controls sync,
   triage and webhooks independently. Import/sync/PR CLI paths use the configured
   GitHub MCP integration. Linking alone does not enable automatic triage.
4. Operator Linear `setup --bootstrap` creates/reuses a project and enables
   daemon synchronization. `--project` selects Gobby; `--project-id` selects
   Linear. Team-only linking does not establish project-scoped synchronization.
5. Linear import requires project scope unless explicitly `--allow-team-wide`.
   `sync-all` pulls then pushes; `--forward` creates/pushes active local work
   without pulling closed history. These can write external issues. Read result
   errors and counts before rerunning; do not infer success from CLI exit alone.
   Pull errors/deferred work suppress the push and preserve the sync cursor.

Use task MCP lifecycle, not operator integration CLI, for agent task mutations.
Do not manually rewrite task storage to resolve sync state. Preserve linkage
and deduplication keys. Repair credentials, repository access or binding before
retrying; inspect reconciliation status for backoff. Do not disable or expand
scope merely to make a command succeed. For webhook recovery load `webhooks.md`.

See [setup and synchronization](../../../../../../../../docs/guides/integrations.md),
[triage recovery](../../../../../../../../docs/guides/github-issue-triage.md#runbook),
and [operator commands](../../../../../../../../docs/guides/cli-commands.md#integrations-and-resource-portability).
