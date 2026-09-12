# Build stages

Load when inspecting or changing lifecycle shape, stage caps, or registry
defaults. Start with `gobby-tasks:list_stages_registry`,
`get_task_type_defaults(task_type=...)`, and `get_task_stages(task_id=...)`.
Read the installed registry and actual manifest; templates and category names
do not prove a task's stage, reviewer, or automation eligibility.

The first non-`done` manifest row is current. Normal states are `ready`,
`in_progress`, `needs_review`, `review_approved`, and `done`. Claims, dependency
blocks, escalation, closure, and automation eligibility remain separate. The
dispatcher selects one deterministic action per task under a dispatch lease.

For lifecycle mutations load the exact tasks reviews reference. It owns
`gobby-tasks-ops:initialize_task_manifest`, `start_stage`, `complete_stage`,
`fail_stage`, `submit_for_review`, `approve_review`, `reject_review`, `add_stage`,
and `remove_stage`. Preserve caller roles, ownership, evidence, review policy,
and retry caps. An agent commits before stage handoff; stage completion and
task closure are distinct. Do not use registry edits as completion evidence.

`update_stage`, `restore_stage`, `delete_stage`, and `set_task_type_defaults`
change registry/default configuration through `gobby-tasks-ops`. Names are
immutable; deleted stages are hidden normally and usage can prevent deletion.
Existing rows retain captured review policy/reviewer metadata. A registry edit
does not retroactively rewrite them. Inspect the selector rather than assuming
all development review uses the same reviewer.

Build stage overrides are validated against installed stages. Skipping stages
normally shapes a new manifest; explicit skips on an existing manifest fail,
while profile skips warn and are ignored. There is a narrow implemented
exception: an epic with expansion output may apply only a `pr` skip and repair
a pristine expanded-epic root manifest under the dispatch mutex. This does not
authorize arbitrary removal of completed or active stages. Deliberate rebuilds
use the recovery topic; future-stage insertion/removal still uses its own gates.

Recovery: read the illegal-transition or mutation detail and refresh current
rows. Preserve completed work and evidence. For planning evidence use the plan
review reference; for epic QA and PR/merge outcomes load their capabilities.

Guides: [Stage registry](../../../../../../../../docs/guides/dispatch.md#stage-registry)
and [Stage reviews](../../../../../../../../docs/guides/orchestration.md#stage-reviews).

_Last verified: 2026-09-12_
