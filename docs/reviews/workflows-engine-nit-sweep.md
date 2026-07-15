# Workflows-engine nit sweep

Revalidated on 2026-07-15 against branch
`review-fixes/16969-workflows-engine` for coordination task #16909. The original
findings remain in [`workflows-engine.md`](workflows-engine.md) as a historical
snapshot. This ledger is the current implementation source of truth.

## Pruned findings

The following original findings no longer belong in the active sweep:

- Legacy `gobby:pipeline-heartbeat` retirement and its stale callable/module
  documentation were completed by #16860. The installed retirement list now includes
  the legacy job.
- Pipeline event callbacks are typed as `Callable[..., Awaitable[None]]`, executor
  event helpers were extracted, and callback failures are suppressed with logged
  tracebacks. The old `Any`, monolith, and narrow catch-list findings are stale.
- `INTERRUPTED` transitions no longer stamp `completed_at`; both individual and bulk
  interruption paths preserve the non-terminal contract.
- JSONB workflow/session-variable decoding is centralized in
  `_decode_variables_payload`, removing the reported row-shape inconsistency.
- Transcript parsing and formatting in `generate_summary` are inside an error
  boundary after #16813. Git/session I/O is also offloaded from the event loop.
- `src/gobby/workflows/CLAUDE.md` was deleted, so its stale module map no longer needs
  a docs leaf.
- Session-variable row cleanup was completed by #16796. The remaining dead
  `delete_variables` API is isolated with related state-manager cleanup below.

## Current residuals

Each independent residual is owned by an unclaimed implementation leaf under #16969:

| Task | Current evidence | Validation focus |
| --- | --- | --- |
| #18298 | Approval rejection in `pipeline/gatekeeper.py:187` and timeout expiry in `storage/pipelines.py:34` cancel executions without closing the child session retained by `pipeline_executor.py:739`. | Retain sessions while waiting; close once on reject/expiry. |
| #18299 | `definitions.py:577` has no exec timeout field, while `pipeline/handlers.py:121` reads an unset context value. | Typed, rendered per-step timeout with a validated default. |
| #18300 | The now-live `pipeline_webhooks.py:138` transport accepts definition-controlled URLs and arbitrary environment-expanded headers without the hardened webhook-executor policy. | SSRF/redirect/response bounds, header policy, explicit methods, observable failures. |
| #18301 | `pipeline_heartbeat.py:132` retains a local asyncio import, duplicate candidate work, and handled-count drift. | One scan per tick and accurate transition metrics. |
| #18302 | `pipeline/renderer.py:24-64` misses exact and non-suffix secret names unless callers provide an allowlist. | Sensitive-name matrix plus explicit allowlist behavior. |
| #18303 | `pipeline/renderer.py:186` coerces string identifiers such as `007` and `1e3` into numbers. | Preserve string intent while retaining native expression types. |
| #18304 | `cli/pipelines.py` still sends no actor for approve/reject audit records. | Stable non-empty CLI actor on both decisions. |
| #18305 | `state_manager.py:54-176` has unstable equal-priority ordering, upsert identity/timestamp drift, and production-dead instance CRUD. | Deterministic ordering, faithful persisted identity/timestamps, pruned dead API. |
| #18306 | `state_manager.py:253-409,608` has stale SQLite transaction docs, fragile set coercion, and a dead deletion API. | PostgreSQL-accurate docs, safe string-set semantics, dead API removal. |
| #18307 | `workflows/constants.py` contains only unused coordinator test literals pinned by vacuous tests. | Remove module/tests and confirm no references. |
| #18308 | `dry_run.py:100,176-199,435,969` still disagrees on workflow type, conflates malformed/missing definitions, and simplifies branching/terminal semantics. | Runtime-aligned types, load diagnostics, paths, and dead ends. |
| #18309 | `summary_actions.py:315` and `hooks/session_summary_dispatcher.py:63` schedule background tasks without lifecycle-owned strong references. | Retain until completion, observe exceptions, release completed tasks. |
| #18310 | `_write_summary_file` documents `full|compact`, while `generate_summary` accepts and writes `clear|compact`. | One public mode vocabulary across behavior, docs, and filenames. |
| #18311 | `webhook_executor.py:318-395` creates a fresh `ClientSession` for every retry attempt. | One bounded session per retry envelope with all hardening preserved. |

## Scope boundary

This coordination pass changes no workflow runtime behavior. Residuals stay isolated
in focused leaves so each behavior change, focused test set, and validation record is
reviewable independently.
