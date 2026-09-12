# Task evidence and artifacts

Load when attaching commits, retrieving task diffs, annotating affected files,
or managing artifact pointers. Discover the tool schema before changing evidence.

On `gobby-tasks`, use `link_commit`, `unlink_commit`, `auto_link_commits`,
`get_task_diff`, and `update_observed_files`. Commit links are evidence, not proof
of completion. Use `close_task(commit_sha=...)` for closing rather than a separate
link followed by close, except when repairing explicitly unlinked tagged commits.

`get_task_diff` is paginated. Follow `byte_end` and both metadata `cursor_end`
values, retaining `snapshot_hash` and `view_hash` on subsequent pages. A byte
page does not prove the commit list or file manifest is complete. Use the
returned opaque path selectors for file views. On stale snapshot/view errors,
restart retrieval; do not join pages from different snapshots.

On `gobby-tasks-ops`, use `set_affected_files`, `get_affected_files`, and
`find_file_overlaps` for declared file scope; `set_artifact`,
`set_artifacts_atomic`, `get_artifacts`, `clear_isolation_pair`, and
`append_description_section` manage typed pointers and idempotent description
sections. These tools enforce caller authority; some are restricted to specific
agent roles. Never bypass denial through SQL, REST, or metadata edits.

Keep coupled artifact fields consistent with `set_artifacts_atomic`. Discover
allowed fields from the schema rather than inventing keys. Clearing an isolation
pair changes task pointers; it does not land or delete the actual worktree or
clone. Use the source-control workflow for workspace lifecycle.

Recovery: inspect task scope, current artifacts, and active owner before replacing
evidence. Preserve foreign paths. Recompute observed files from the intended
commit set when evidence and declared scope disagree, and explain legitimate
scope changes instead of weakening criteria.

Guide: [Git and Validation](../../../../../../../../docs/guides/tasks.md#git-and-validation).

_Last verified: 2026-09-12_
