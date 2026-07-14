# Servers/routes nit sweep

Revalidated on 2026-07-14 against branch `review-fixes/16955-servers-routes` for
coordination task #16794. The original findings remain in
[`servers-routes.md`](servers-routes.md) as a historical snapshot. This ledger is the
current implementation source of truth.

## Pruned findings

The following original nit groups no longer belong in the active sweep:

- Raw 500 exception leakage duplicates completed leaf #16628.
- Degraded branch-list caching duplicates completed leaf #16645.
- Event-loop storage/subprocess work duplicates completed leaf #16661.
- Auth middleware exception behavior and login/cookie notes were superseded by
  completed auth leaves #16492, #16558, #15961, and the AuthService rewrite.
- Admin restart ordering, subprocess offload, and lock cleanup duplicate completed
  leaf #16447.
- Workflow template drift and restore collisions duplicate completed leaf #16499.
- File read bounds duplicate completed leaf #15952.
- Pipeline response/error leakage and approval-token notes duplicate completed leaves
  #15970 and #16628.
- Configuration import/value/template residuals duplicate completed leaves #16184,
  #16521, and #16577.
- Findings tied to deleted or materially rewritten route shapes (`stage_routes.py`,
  retired savings routes, old pipeline response models, old auth helpers, and old MCP
  route line references) were removed instead of carrying obsolete paths forward.

## Current residuals

Each independent residual is owned by an unclaimed P4 leaf under #16955:

| Task | Current evidence | Validation focus |
| --- | --- | --- |
| #18198 | `src/gobby/servers/routes/tasks_stage_routes.py:152` still drops request-session attribution on ordinary stage mutations. | Resolve local refs/UUIDs and cover every mutating action. |
| #18199 | `src/gobby/servers/routes/tasks_lifecycle_routes.py:35` accepts an empty `commit_sha`, and `close_task` still branches on truthiness. | Reject empty/whitespace SHAs while preserving valid commit closes. |
| #18200 | `src/gobby/servers/routes/files.py:131` retains broad git fallback catches and `:325` serves SVG inline. | Safe SVG delivery plus typed expected-failure fallbacks. |
| #18201 | `src/gobby/servers/routes/source_control.py:364,686` and `src/gobby/servers/routes/memory.py:387` accept unbounded numeric limits. | Reject negative/over-ceiling values at the HTTP boundary. |
| #18202 | `src/gobby/servers/routes/workflows.py:258` toggles with a non-atomic read-modify-write. | One storage mutation and concurrent-toggle coverage. |
| #18203 | `src/gobby/servers/routes/admin/_lifecycle.py:198,299` returns HTTP 200 for shutdown/reload failures. | Stable non-2xx failure responses and unchanged success contracts. |
| #18204 | `src/gobby/servers/routes/admin/_testing.py:37` uses check-then-insert project registration. | Conflict-safe concurrent registration. |
| #18205 | `src/gobby/servers/routes/configuration_prompts.py:31` reports a page-local prompt `total`. | Filter-scoped total independent of `limit`/`offset`. |
| #18206 | `src/gobby/servers/routes/sessions/core.py:376` duplicates pruning already performed by statusline activity tracking. | One prune per update with unchanged timestamps. |
| #18207 | `src/gobby/servers/routes/agents.py:697` can kill an agent before a failing manager update leaves durable state inconsistent. | Failure-path state reconciliation. |
| #18208 | `src/gobby/servers/routes/build.py:85` accepts unknown control fields and supplied-option detection conflates omission with explicit defaults. | Strict request validation and Pydantic field-set semantics. |
| #18209 | `src/gobby/servers/routes/tasks.py:206` and `src/gobby/servers/routes/projects.py:65` retain per-row owner/stat lookups. | Constant query counts as list size grows. |
| #18210 | `src/gobby/servers/routes/cron.py:23` can persist an omitted project as an empty string. | Resolve the current project or return a clear 4xx. |

## Scope boundary

This coordination pass changes no route behavior. Current residuals stay isolated in
the focused leaves above so their code, tests, validation evidence, and commits remain
reviewable by domain.
