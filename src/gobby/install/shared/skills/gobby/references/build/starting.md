# Starting a build

Load before launching or changing launch options. Discover the target with
`gobby-tasks:get_task(brief=false)` or the plan registry, then read build status,
dependencies, and stages. For a raw idea load the plan drafting reference first.
Do not create a duplicate tree when the plan already has an open build root.

Use `gobby-tasks-ops:build_task` after loading its schema. Classify input as a
task reference or existing Markdown plan path; paths resolve in the selected
project. Collect only missing information and requested overrides. Present the
concrete target/options before launch; existing user authorization suffices.
Ask only for a required unresolved choice or authority, not repeated approval.

Resolve the installed profile (default name `default`), isolation, delivery
intent, stage settings, agent, target branch, coordinator, and concurrency.
Use the profiles topic for precedence. `isolation` is `none`, `worktree`, or
`clone`; reject conflicting isolation aliases. `no_merge` requires an isolated
workspace. `stage` accepts selectors/settings such as
`development:max_review_rounds=4`. `max_retries=0` means one work attempt.
`max_active_agents` bounds the immediate heartbeat, not the persistent daemon cap.

An authorized preview can use `build_task(input_ref="<target>", dry_run=true)`.
It rolls back task changes and suppresses dispatch/workspace side effects. Do
not count the preview as execution. Normal launch enables automation, records
history/artifacts, resolves a manifest, and kicks dispatch. `quick=true` permits
one bounded lifecycle action and leaves target automation disabled afterward;
it is a smoke check, not unattended E2E validation.

Plan-file launch defaults to `planning_seed_state=drafted`. Seed `needs_review`
or `approved` only from real plan evidence; never invent completed review rounds.
Approved input begins at expansion. Load the plan approval/expansion references
for their contract. Enhancement rounds are separate from adversary rounds and
require the user's applicable authorization. `reset_expansion_output` deletes
generated work; inspect the expansion recovery topic before requesting it.

For cross-project requests pass the target `project_id` and a full coordinator
session UUID. Operator CLI `--coordinator current` resolves `GOBBY_SESSION_ID`;
explicit `--project` rejects local coordinator refs because their project is
ambiguous. Do not assume the MCP caller is automatically the build coordinator.

Report the returned task ID, created/existing root, initial lifecycle, manifest,
skipped stages, warnings, and dispatcher outcome. A successful request is not a
completed build. If validation fails, retain the requested options, inspect the
error/status, and use recovery guidance before retrying.

Guide: [Build entry points](../../../../../../../../docs/guides/dispatch.md#entry-points).

_Last verified: 2026-09-12_
