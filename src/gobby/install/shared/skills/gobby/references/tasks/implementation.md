# Implement and verify claimed work

Load before editing task-owned files, handling found work, or preparing validation.
Read `get_task(brief=false)` and applicable required instruction references first.
Discover `inspect_task_path_ownership` and `release_task_paths` on `gobby-tasks`
when attribution or shared-checkout ownership is unclear.

Keep one deliverable's implementation substeps in the provider's native tracker.
Inspect current changes before edits and preserve foreign work. Name the test
level and smallest complete verification scope before implementation. Run focused
tests for behavior changes; the full pytest suite requires an explicit request.
Use isolated fixtures or temporary daemons, never the user's live state.
Check production file line counts before edits: hand-maintained source must stay
below 1,000 lines. Load standalone `decompose-monolith` for threshold-crossing
work and complete that decomposition within this task; do not defer it.

For TDD-required tasks, load standalone `test-driven-development` before edits.
Preserve red, minimal green, refactor/final-green, exact command, and applicable
test-audit evidence. A test-write nudge is not proof that TDD was completed.
For development obligations use `$gobby development`.

Every encountered defect, warning, or failed check becomes work in this session:

1. Fix and verify it inside the current task, tracking it with the other substeps.
2. If its files or work belong to another active session, send that owner the
   command, diagnostics, paths, and impact through `gobby-agents:send_message`.
   Spawned agents route owner handoffs through `send_message(target="parent")`.
   Preserve their files and prove the failure is confined with a passing scoped run.
3. Only a genuine decision, necessary planning pass, or broad clean window permits
   filing `needs-decision`, `needs-planning`, or `clean-window`, with the reason
   in the description. Filing alone does not finish found work.

Restarts and rebuilds are coordination within the fix, not deferral reasons.
Enhancement ideas without broken behavior may be recorded as ideas normally.

Run final validation after every final edit and formatting change. Follow an
ongoing command to definitive exit. Direct commands with environment prefixes
are credited; pipelines, fallbacks, backgrounding, subshells, and trailing output
can hide the validation outcome and require a direct rerun. A later edit makes
prior evidence stale; committing does not.

If a close reports stale foreign attribution, the owning session can call
`release_task_paths` for committed or abandoned paths. It refuses uncommitted
content; do not release another session's ownership yourself.

Guides: [Git and Validation](../../../../../../../../docs/guides/tasks.md#git-and-validation),
[TDD Enforcement](../../../../../../../../docs/guides/tdd-enforcement.md).

_Last verified: 2026-09-12_
