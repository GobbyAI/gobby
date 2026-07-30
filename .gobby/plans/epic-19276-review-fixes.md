Plan artifact: `.gobby/plans/epic-19276-review-fixes.md`

> **Plan ID:** epic-19276-review-fixes

# Epic #19276 Review Fixes

## Overview
`kind: framing`

Autonomous agents closed all 15 children of epic #19276 (plan-review stress-test defects) across 14 commits. A three-reviewer commit audit, spot verification of every load-bearing claim, and a 724-test sweep found that most commits fixed a symptom adjacent to their finding: two runtime ship-blockers, one latent regression, and two "enforcement" fixes that enforce nothing. Three rounds of manual Codex review corrected mechanisms in this plan that would have recreated the same failures. This epic repairs the repairs, then proves the machinery end-to-end by re-running the web-styling plan through two unattended adversarial review rounds.

Commit verdicts from the audit (all claims verified against source):

| Commit | Task | Verdict |
| --- | --- | --- |
| `05404c85e` #19284 MCP identity | sound, undertested (e2e test passes pre-fix) |
| `1f9c993ac` #19289 credentials | gap: 2 ship-blockers + token leaks; F13 not closed |
| `8ac3f3f7b` #19285 retry loop | poor: prose-only stop, F19 silently dropped |
| `24a72bf84` #19286 vote artifacts | gap: write-only — zero consumers, forgeable decisions |
| `c6a622ece` #19287 enhancer | gap: prose-only + new internal contradiction |
| `86bdceb1d` #19288 write gating | sound on root cause; `git apply`/`patch` still ungated |
| `bfa96d757` #19290 claim loss | gap: wrong layer — guard is dead code, real mechanism unpatched |
| `9b7f0e3a1` #19291 review binding | poor: fixes interactive, silently breaks staged mode |
| `dffd33bd3` #19292 truncation | gap: fixed the 24KB cap; actual 300-char truncator untouched |
| `b7664e43d` #19304 requirements | gap: accumulation right; observer crash path + span leak + no bound |
| `bd44f74ec` #19306 attribution release | gap: works, but instruction truncated away; release unverified |
| `3a0d5d8fb` #19309 plan-mode nudges | sound + user-confirmed contract; record conflict with #19216 |
| `c0749939d` #19317 heartbeat claims | landed post-audit: `handoff_ready` alive in `_is_session_alive` with regression test — verified this pass; V2 re-runs its test |
| `488cf2d14`/`c915706fd` #19276 direct | partial: skills ledger unbounded; actor naming 2-of-7 surfaces |

## Constraints
`kind: framing`

- Sandboxed agents keep access to the daemon and the Rust binaries (ghook/gcode/gwiki), including a future docker backend with network access. Credential fixes extend the scoped capability to those consumers — never wall the sandbox off. Backend-agnostic: token in env, daemon reachable over network, no loopback or host-file dependence, no tmux-specific mechanisms.
- No truncation of diagnostic or actionable content anywhere: preserve in full and reference; never cut.
- Nothing is deferred. Every finding, nit included, is fixed in-session before the epic closes. The single pre-declared scope boundary: the sandbox still read-grants `~/.gobby/bootstrap.yaml` (plaintext hub DSN) and the secret-store KEK symlinked into runtime homes, with loopback open — a higher-privilege bypass than F13 itself; gcode requires direct Postgres today. That redesign (scoped DB credential or daemon-proxied gcode DB access) is tracked as a 0.5 release blocker task, and this epic's security claim is scoped to operator-token removal from the sandbox, capability lifecycle, and toolchain restoration, with the residual risk recorded in the epic description.
- Swarm execution with an explicit review lifecycle: subagents implement; the coordinator reviews the precommit diff and evidence while the author session is live; the author fixes and commits; the author submits for review; the coordinator independently validates and approves or rejects; the coordinator closes only through the review workflow. Exception: P1 is coordinator-implemented (its subject is the spawn machinery the swarm itself needs).
- The coordinator owns every bug found along the way, whoever authored it. Diagnostics confined to a subagent-owned dirty path are routed to that owner via P2P while it is live; coordinator accountability remains.
- Two adversarial review rounds run unattended (explicitly user-authorized; the standing default is interactive per round). They are validation, not completion: after round 2 the coordinator stops and the user decides whether to run more rounds. The plan is not expanded and not declared done.
- The coordinator calls `compact_self` between adversarial rounds per the plan skill.
- Plan-mode contract (user-confirmed): plan mode gets no context-pressure nudges, ever; the only future exception is autonomous plan-to-PR via `gobby build`, which is what the `is_spawned_agent` conditional is reserved for.
- `compact_self`'s `rule_name` parameter stays exactly as is (user decision). It is rule-attribution plumbing; manual invocations omit it.
- Bundled rule/workflow/skill YAML changes take effect only after `gobby install`/sync refreshes DB rows; P7 owns that step.
- Cross-cutting test bar: tests assert the post-adapter / post-observer surface the agent actually sees, never only the raw internal string.

## P1: Scoped-Capability Hardening
`kind: framing`

**Goal**: The scoped agent capability (`GOBBY_AGENT_API_TOKEN`) works for the whole in-sandbox toolchain — Rust binaries, `gobby` CLI, MCP subprocess — with no secret in argv, config values, or persisted metadata, and with expiry, liveness, and rotation. Coordinator-implemented: the swarm cannot be spawned through the broken mechanism this phase repairs. Merges rev-1 T1+T2+T13 — one capability boundary, sequenced closures would permit incompatible intermediate states.

### 1.1 Prefer the env capability in gobby-core credential resolution [category: code]
`kind: deliverable`

Target: `crates/gcore/src/local_token.rs`

`crates/gcore/src/local_token.rs:8-21` is the sole credential source for ghook, gcode, and gwiki, and no Rust code reads `GOBBY_AGENT_API_TOKEN`. With `auth_mode: "required"` (the default) and the sandbox denying `~/.gobby/local_cli_token`, sandboxed ghook posts unauthenticated → 401 → fail-safe exit 2 blocks every critical hook (`crates/ghook/src/action.rs:160-180`); gcode graph lifecycle (`crates/gcode/src/graph/code_graph/lifecycle.rs:257`) and gwiki (`crates/gwiki/src/daemon.rs:93`) hard-error.

- Modify the token-resolution function so it prefers the `GOBBY_AGENT_API_TOKEN` environment variable and falls back to the token file. ghook/gcode/gwiki inherit through the shared function; no per-binary changes.
- Rust unit tests: env set → env token wins; env absent → file; both absent → error path unchanged.
- Load the `rust` skill before editing. Rebuild is not live until reinstalled — installation happens in 1.6, after the whole P1 contract passes.

**Acceptance:**

- 1.1.1 - Credential resolution prefers the env capability with file fallback. file: `crates/gcore/src/local_token.rs`.
- 1.1.2 - Env-preference and fallback behavior are unit-tested in the crate. test: `crates/gcore/src/local_token.rs::env_capability_preferred`.

### 1.2 One capability matrix and correct identity matching [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/auth_service.py`
- `src/gobby/mcp_proxy/stdio_proxy.py`
- `tests/servers/test_auth_service.py`

`_agent_capability_allows` (`auth_service.py:43-61`) covers only MCP discovery/call, hook execution, and codewiki refresh, so ~20 `gobby` CLI surfaces backed by `DaemonClient` 401 inside agent sessions (`daemon_auth_headers` prefers the env token unconditionally, `src/gobby/utils/local_token.py:110-115` — that preference is correct and stays). `_agent_identity_matches` (`auth_service.py:64-75`) compares the target-project header (`X-Gobby-Project-Id` is a documented target override per `request_context.py:170-177`) and the raw unresolved `session_id` ref, so documented cross-project `call_tool` and `#N` self-refs 401 — unactionable retry-bait, the F12/F18 failure mode reborn.

- Replace the two-style route check with one enumerated method+route capability matrix in `auth_service.py`, the single source of agent capability: MCP discovery/call, hook execution, codewiki refresh, plus the read-only routes the Rust binaries and `gobby` CLI legitimately hit from inside a run (enumerate by auditing `DaemonClient` call sites and the three crates' endpoints). Agents may use the read-only CLI surface; operator mutation/config routes stay excluded. Exact-match path style throughout.
- Modify `_agent_identity_matches` to compare claims against `X-Gobby-Caller-Project-Id` (falling back to the target header) and to compare resolved session UUIDs — resolve the ref before the identity comparison, or perform the check after context resolution.
- Update `stdio_proxy.py` so the session header carries the resolved UUID.
- Tests: cross-project `call_tool(project_id=...)` authenticates; a `#N` self-ref authenticates; out-of-matrix routes still 401; response-confidentiality for MCP server-listing routes — agent responses redact configured environment and header values.

**Acceptance:**

- 1.2.1 - One enumerated capability matrix governs agent routes. symbol: `_agent_capability_allows`.
- 1.2.2 - Cross-project and `#N`-ref calls authenticate; out-of-scope routes are rejected. test: `tests/servers/test_auth_service.py::test_agent_capability_matrix`.
- 1.2.3 - Server-listing responses redact env and header values for agent callers. test: `tests/servers/test_auth_service.py::test_agent_listing_redaction`.

### 1.3 Secret hygiene: env_vars passthrough, resume allowlist, inbox actor [category: code]
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor_support.py`
- `src/gobby/agents/resume_metadata.py`
- `src/gobby/hooks/inbox.py`
- `src/gobby/cli/installers/git_hooks.py`
- `tests/agents/test_spawn_executor.py`

The token value currently transits argv (`-c mcp_servers.gobby.env.GOBBY_AGENT_API_TOKEN=<value>`, `spawn_executor_support.py:274-277`, `ps`-visible to sibling sandboxes) and is persisted verbatim in `agent_runs.resume_metadata_json` via `config_overrides` (`spawn_executor_support.py:146`), beside `merge_resume_metadata_env` whose contract is "non-secret env values".

- Modify the Codex override builder to pass `GOBBY_AGENT_API_TOKEN` through Codex's `mcp_servers.<id>.env_vars` whitelist (named variables forwarded from the parent environment into the stdio MCP child — see the Codex configuration reference). The value exists only in process environment; the identity overrides (`GOBBY_SESSION_ID`, `GOBBY_AGENT_RUN_ID`, `GOBBY_PROJECT_ID`) stay as literal `env` values since they are not secrets. No launcher, no credential file — both violate the backend-agnostic constraint.
- Modify resume-metadata persistence to a typed positive allowlist of enumerated non-secret override keys; resume re-mints the capability, never replays a stored one.
- Modify `inbox.py` `_post_envelope` (line 154) back to the operator token: it runs inside the daemon over ASGITransport, and an inherited agent env would drain the hook inbox with a scoped token and 401 other sessions' envelopes.
- Modify `git_hooks.py` lines 54-60 to `${VAR:-}` expansions (bare `$GOBBY_*` aborts under `set -u`; line 46 is already correct).
- Tests: assert token absence from argv, config override values, and persisted resume metadata; assert `env_vars` carries the name.

**Acceptance:**

- 1.3.1 - The token value is absent from argv, config values, and resume metadata; `env_vars` forwards it by name. test: `tests/agents/test_spawn_executor.py::test_capability_token_never_in_argv_or_metadata`.
- 1.3.2 - Resume metadata persists only allowlisted non-secret keys and re-mints on resume. file: `src/gobby/agents/resume_metadata.py`.
- 1.3.3 - The inbox poster uses the operator token. file: `src/gobby/hooks/inbox.py`.

### 1.4 Capability lifecycle: expiry, run liveness, no hook exemption [category: code]
`kind: deliverable`

Targets:
- `src/gobby/utils/local_token.py`
- `src/gobby/servers/auth_service.py`
- `tests/servers/test_auth_service.py`

`AgentApiTokenClaims` (`local_token.py:20-30`) has no `exp`/`iat` and no revocation: a dead run's token works forever, and `/api/hooks/execute` is exempt from the run-id check (`auth_service.py:73-74`), so a stale token can forge hook events indefinitely — a step-enforcement bypass.

- Add `exp` and `iat` to the claims with deterministic values: when the run declares `timeout_seconds`, `exp = iat + timeout_seconds + 60`-second fixed grace; untimed runs (`timeout_seconds` is nullable and agent health checks skip them — no run-timeout fallback exists today; the sole 1800 constant in the tree is the WebSocket idle timeout) get a new fixed ceiling constant `AGENT_TOKEN_MAX_TTL_SECONDS = 86400`. Expiry is defense-in-depth; the per-request run-liveness check is the real revocation, so the ceiling never strands a legitimate long run mid-flight, and resume re-mints a fresh capability (1.3).
- `_verify_agent_request` rejects expired tokens and checks run liveness on every request.
- Remove the `/api/hooks/execute` run-id exemption: ghook has the run id in its spawn env and sends it.
- Negative tests: expired token rejected on both the explicit-timeout and untimed-ceiling paths; terminal-run token rejected; resume re-mints instead of replaying; hook post without matching run identity rejected.

**Acceptance:**

- 1.4.1 - Claims carry expiry and issuance; verification enforces both plus run liveness. symbol: `AgentApiTokenClaims`.
- 1.4.2 - The hooks route requires matching run identity. test: `tests/servers/test_auth_service.py::test_hooks_route_requires_run_identity`.

### 1.5 Honest propagation coverage for #19284 [category: test]
`kind: deliverable`

Targets:
- `tests/agents/test_spawn_executor.py`
- `tests/e2e/test_stateless_ambient_session.py`
- `src/gobby/agents/spawn_executor_support.py`

The #19284 e2e test (`test_stateless_ambient_session.py:287-364`) passes identically pre-fix — it seeds env via monkeypatch and never exercises the propagation; the only real coverage is an argv string assertion. `test_spawn_executor.py:573` asserts `GOBBY_PARENT_SESSION_ID` absence with no stated reason (the reason is real: leases must resolve to the child session, never the parent).

- Add a seam test: build the child environment only from the Codex override/`env_vars` model (the scrub model), then assert `DaemonProxy` emits the child's `X-Gobby-Session-Id` so the `get_tool_schema` lease and the following `call_tool` resolve to the same session.
- Rename or annotate the e2e test so it reads as daemon-side non-regression, not fix coverage.
- Add the explanatory comment for the deliberate `GOBBY_PARENT_SESSION_ID` withholding at the assertion and in `spawn_executor_support.py`.

**Acceptance:**

- 1.5.1 - A seam test drives the scrub model end-to-end to the proxy's session header. test: `tests/agents/test_spawn_executor.py::test_scrubbed_child_env_reaches_daemon_proxy_identity`.
- 1.5.2 - The parent-session withholding is documented at both sites. file: `src/gobby/agents/spawn_executor_support.py`.

### 1.6 Bootstrap gate: install, restart, rotate, smoke [category: test] (depends: 1.1, 1.2, 1.3, 1.4, 1.5)
`kind: deliverable`

Target: `.gobby/plans/epic-19276-review-fixes.md`

P1's completion gate, run by the coordinator (this is what makes swarm execution possible; nothing else in the epic starts until it passes):

1. Drain or handle live agent runs.
2. `cargo test -p gobby-core -p gobby-hooks`; rebuild and reinstall `~/.gobby/bin/{gcode,ghook,gwiki}`; `uv run gobby install` to sync bundled rows.
3. Restart the daemon onto the new verifier.
4. Rotate the operator token per the auth runbook (every scoped token is signed from it, and F13's premise is that sandboxed agents already read the old one): rotate, then assert the old operator token and an old-format agent token both fail.
5. Live sandbox smoke: spawn a sandboxed agent whose only credential is the env capability (token file unreadable) — hooks deliver, `gcode` graph commands work, `call_tool` round-trips through the proxy, and a cross-project `call_tool(project_id=...)` succeeds.
6. Record the gate transcript in this plan's changelog. Swarm execution is enabled only after step 5 passes.

**Acceptance:**

- 1.6.1 - The bootstrap gate transcript (install, restart, rotation with old-token failures, sandbox smoke) is recorded. behavior: "P1 bootstrap gate transcript" in `.gobby/plans/epic-19276-review-fixes.md`.

## P2: Claim Integrity
`kind: framing`

**Goal**: `claimed_by_session_id` can no longer be cleared silently: the database refuses cascaded clears and every legitimate release is centralized and audited. The third leg — the heartbeat treating mid-compaction sessions as alive — landed post-audit as #19317 (commit `c0749939d`, verified during this planning pass with its regression test in place), so this phase carries no leaf for it; V2 re-runs its focused test.

### 2.1 FK RESTRICT migration and guarded session delete [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/migrations/349_task_claim_fk_restrict.sql`
- `src/gobby/storage/postgres_baseline_schema.sql`
- `src/gobby/storage/sessions/_identity_crud.py`
- `tests/storage/sessions/test_delete_guard.py`

The #19290 fix patched `sweep_stale_claims` — provably not F17's mechanism, since the sweeper always bumps `tasks.updated_at` (`src/gobby/storage/tasks/_automation.py:142-144`) while the finding's forensics showed the claim cleared without a bump. The only matching writer is the FK `claimed_by_session_id ... ON DELETE SET NULL` (`postgres_baseline_schema.sql:374`) firing on session-row delete; `SessionManager.delete()` (`_identity_crud.py:80-88`) is a bare `DELETE` with no reference guard — an asymmetry the standing memory review had already flagged.

- Create tracked migration `349_task_claim_fk_restrict.sql` (migration numbers pre-allocated for parallel fan-out: 349 here, 350 reserved for 4.1) changing the FK to `ON DELETE RESTRICT`; update the baseline schema to match. An application guard cannot prevent other deletion paths from firing the cascade; RESTRICT can.
- Modify `SessionManager.delete()` to check `session_has_retained_references` first and return an actionable error naming the retained references, instead of surfacing a raw FK violation.
- Tests: deleting a session with a live claim errors actionably; the guarded ghost-delete and prune paths still work; RESTRICT blocks a direct SQL delete.

**Acceptance:**

- 2.1.1 - The FK restricts deletes and the baseline matches the migration. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 2.1.2 - Guarded delete returns an actionable error for sessions holding claims. test: `tests/storage/sessions/test_delete_guard.py::test_delete_refuses_claim_holder`.

### 2.2 Centralized audited claim release; dead guard removed [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/tasks/_automation.py`
- `src/gobby/sessions/compact_continuation.py`
- `src/gobby/sessions/compact_markers.py`
- `src/gobby/workflows/engine/effects.py`
- `tests/storage/tasks/test_sweep_stale_claims.py`

- Extract one explicit claim-release function; the legitimate release paths (orphan-handoff expiry, stale sweep) call it, and each writes an audit log line naming the actor and reason — the original #19290 criteria demanded the clearing mechanism be *named*, and it still is not.
- Remove the dead sweeper guard from `_automation.py`: its 600-second marker window can never overlap the 30-minute orphan-handoff expiry, so it never fires. With RESTRICT (2.1) plus centralized release it has no job.
- Create `compact_markers.py` as a leaf module holding the `COMPACT_SELF_CONTINUE_*` constants; move them out of `compact_continuation.py` so the storage layer stops importing the sessions layer at module scope (`_automation.py:5-8`) and `effects.py` drops its lazy-import dodge (lines 340-342).

**Acceptance:**

- 2.2.1 - Every claim-release path routes through one audited function. symbol: `release_task_claim`.
- 2.2.2 - The dead compact-marker guard is gone and sweep behavior is pinned. test: `tests/storage/tasks/test_sweep_stale_claims.py`.
- 2.2.3 - Shared constants live in a leaf module imported by both layers. file: `src/gobby/sessions/compact_markers.py`.

## P3: Review Binding and Request Anchor
`kind: framing`

**Goal**: `prepare_plan_review_round` binds correctly in both interactive and staged modes, and the plan-mode request anchor is robust, lossless, and span-scoped.

### 3.1 Restore staged binding without losing the envelope fix [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/plans/review_evidence.py`
- `src/gobby/mcp_proxy/tools/plans/vote_artifacts.py`
- `src/gobby/mcp_proxy/tools/review_learning.py`
- `tests/mcp_proxy/test_plans_tools.py`

The #19291 fix defaults `session_id or get_current_session_id()` unconditionally (`tools/plans/review_evidence.py:161`) against the service's strict XOR (`src/gobby/plans/review_evidence.py:804-817`), so a staged caller (`task_id`+`stage`) through the MCP tool always fails `invalid_attempt_binding`. Wrapper identity is caller context; body `session_id` is a target-tool argument — generic envelope injection into optional properties would insert the ambient session before the tool sees `task_id`+`stage` and recreate the failure, so `_schema_requires_session_id` (`src/gobby/mcp_proxy/services/tool_execution.py:113`) keeps its required-fields-only contract, untouched.

- Modify `prepare_plan_review_round`'s ambient fallback to fire only when `task_id` and `stage` are both absent.
- Modify the sibling tools with optional `session_id` to explicit per-tool policies with docstrings: `vote_artifacts.py:199` and `review_learning.py:56` are interactive-only, plain ambient fallback.
- Tests: staged binding with ambient context set binds staged with no error; interactive envelope binding (existing) still passes; an explicit argument wins over ambient.

**Acceptance:**

- 3.1.1 - Staged callers bind through the MCP tool with ambient context present. test: `tests/mcp_proxy/test_plans_tools.py::test_prepare_review_round_staged_binding_with_ambient_context`.
- 3.1.2 - Sibling optional-session tools declare explicit binding policies. file: `src/gobby/mcp_proxy/tools/plans/vote_artifacts.py`.

### 3.2 Request-anchor robustness with lossless migration [category: code]
`kind: deliverable`

Targets:
- `src/gobby/plans/review_requirements.py`
- `src/gobby/workflows/observer_plan_mode.py`
- `src/gobby/plans/review_terminal.py`
- `tests/plans/test_review_requirements.py`
- `tests/workflows/test_observers_detection.py`

Live-confirmed in daemon logs (recurring `Observer resolve_plan_mode failed ... request anchor does not match the canonical version-2 schema`, sessions `fa12f930` and `d943cbc5`): `append_request_anchor` raises on missing or v1 anchors (`review_requirements.py:62,125`), and `resolve_plan_mode` calls it bare (`observer_plan_mode.py:281`) before the mode-state writes, so affected plan-mode sessions silently stop reconciling every turn.

- Modify the anchor paths so the observer never raises. Migration is lossless (no-truncation policy): a valid v1 anchor's content is retained as the first entry of the v2 list; malformed raw content is preserved verbatim in a `migration_evidence` field, never discarded; the cleaned substantive current message is appended. A missing anchor plus a noise-only message leaves the anchor absent while observer state reconciliation continues. "Noise-only" is decided by a conservative normalized-acknowledgement predicate: casefold, trim, strip terminal punctuation, then exact-match against a fixed set (`ok`, `okay`, `k`, `y`, `yes`, `yep`, `yeah`, `sure`, `go`, `go ahead`, `proceed`, `continue`, `do it`, `sounds good`, `lgtm`, `approved`, `thanks`, `thank you`, `ty`, `👍`); only exact-set matches count as noise — everything else, "ok but rename the flag" included, seeds or appends to the anchor byte-for-byte.
- Modify the append path to use the cleaned prompt (not raw, `observer_plan_mode.py:390-398`), strip `<system-reminder>`/`<conversation-history>` wrappers, and skip empty or whitespace-only messages. Noise filtering only — no substantive message is ever dropped.
- Modify plan-mode exit (`is_plan=False`) to clear `REQUEST_ANCHOR_VARIABLE`; re-entry with `request_content=None` must not adopt a prior plan's anchor.
- Modify the `needs_requirements` retry (`review_terminal.py:242-249`) to require a changed anchor `content_sha256`; an unchanged anchor routes to the existing `needs_requirements:` escalation contract instead of re-preparing.
- Modify bundle rendering so anchor content is newline-joined per-message with delimiters (line-addressable citations); canonical JSON is used only for hashing.
- Tests: missing anchor, v1 anchor (content retained), malformed anchor (evidence preserved), every acknowledgement-set member plus near-miss non-members, cross-plan re-entry, unchanged-anchor escalation, multi-line citation spans.

**Acceptance:**

- 3.2.1 - The observer reconciles plan-mode state for every anchor shape without raising. test: `tests/workflows/test_observers_detection.py::test_resolve_plan_mode_tolerates_v1_and_missing_anchor`.
- 3.2.2 - v1 content and malformed raw content survive migration byte-for-byte. test: `tests/plans/test_review_requirements.py::test_anchor_migration_is_lossless`.
- 3.2.3 - An unchanged anchor escalates instead of looping. file: `src/gobby/plans/review_terminal.py`.
- 3.2.4 - The acknowledgement predicate is exact-set only; near-miss messages seed or append byte-for-byte. test: `tests/plans/test_review_requirements.py::test_ack_predicate_exact_set`.

## P4: Vote Gate and Enhancer Contract
`kind: framing`

**Goal**: Vote artifacts become enforcement: transactional, bound to evidence, with two authenticated provenance paths — observed user interaction and coordinator decision — and the enhancer contract gains its mechanical detector.

### 4.1 Transactional vote gate bound to evidence [category: code] (depends: P3)
`kind: deliverable`

Targets:
- `src/gobby/plans/vote_artifacts.py`
- `src/gobby/mcp_proxy/tools/plans/vote_artifacts.py`
- `src/gobby/workflows/observer_mcp.py`
- `src/gobby/plans/review_evidence.py`
- `src/gobby/plans/review_evidence_models.py`
- `src/gobby/plans/review_evidence_store.py`
- `src/gobby/storage/migrations/350_plan_review_evidence_artifacts.sql`
- `src/gobby/storage/postgres_baseline_schema.sql`
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `src/gobby/install/bundled_content_manifest.json`
- `tests/plans/test_vote_artifacts.py`
- `tests/skills/test_plan_review_skill.py`

`plan_vote_artifacts` is write-only — two writers, one lister, zero consumers (verified by full-repo search): no rule, no gate, no evidence link. The observer receipt (`observer_mcp.py:58-72`) stores only agent-authored `tool_input` and discards `tool_output`, so fabricated decisions are indistinguishable from real ones — F11's exact complaint, reproduced one layer up. A `before_tool` write block plus post-write substring check would be non-atomic; the gate belongs in the evidence/stage transaction with the established stage-approval lock ordering.

- Move artifact storage into the `plan_review_evidence` row (kills the session-variable cap-50 eviction and the false "durable" claim). The table has no artifact or receipt columns today: migration `350_plan_review_evidence_artifacts.sql` adds them, the baseline schema is updated to match, and `review_evidence_models.py`/`review_evidence_store.py` carry the typed fields and accessors with storage tests. Artifact writes share the evidence/stage transaction and its established stage-approval lock ordering.
- Modify the observer receipt to store `evidence_id`, `round_number`, `round_kind`, pre-plan `content_sha256`, `captured_by`, and the canonical normalized `tool_output` stored inline with its digest — no pointer branch. Receipts are validated and consumed inside the transaction that records the artifact.
- Require each vote's `decision` to be derivable from the stored observed answer for its `finding_id`.
- Add a second provenance path: `coordinator_decision`, accepted only from an operator-authenticated request — agent-capability tokens are rejected outright — whose resolved caller session equals the evidence row's `session_id`; it persists canonical decisions plus digest, labeled coordinator-authored. User-interaction provenance stays separate. The plan skill's round protocol is updated to invoke `coordinator_decision` in unattended rounds (with bundled-manifest regeneration), pinned by a skill contract test. This is what 7.2 exercises — without it, unattended review and interaction-only artifacts are contradictory contracts.
- Gate the round's stage transition/finalization on a matching artifact, and validate in the same transaction that every accepted vote's `proposed_edit_text` is present in the post-fold-in plan under a defined canonicalization: whitespace-normalized block match against a stable target-section identity recorded as `target_section_id` on each accepted-edit record, so accepted text in comments or unrelated sections cannot satisfy validation. This is simultaneously F7's mechanical detector and the artifact's first consumer.
- Fix the escaping bug: the provenance check compares raw strings to a JSON-escaped dump (`tools/plans/vote_artifacts.py:246-264`), falsely rejecting any multi-line or quoted text; compare against the payload's string leaves. Add multi-line and quoted-text tests (current tests are all single-line ASCII).

**Acceptance:**

- 4.1.1 - Artifacts persist in the evidence row and gate the round's stage transition transactionally. symbol: `plan_review_evidence`.
- 4.1.2 - Votes bind to stored observed output; fabricated decisions are rejected. test: `tests/plans/test_vote_artifacts.py::test_vote_decisions_bind_to_observed_output`.
- 4.1.3 - Coordinator-authored decisions are authenticated, persisted, and labeled. test: `tests/plans/test_vote_artifacts.py::test_coordinator_decision_provenance`.
- 4.1.4 - Multi-line and quoted finding text passes provenance verification. test: `tests/plans/test_vote_artifacts.py::test_multiline_payload_verification`.
- 4.1.5 - Accepted edit text must match the target section under canonicalization. test: `tests/plans/test_vote_artifacts.py::test_fold_in_requires_section_match`.
- 4.1.6 - Artifact and receipt columns exist via migration 350 with matching baseline, typed models, and store accessors. file: `src/gobby/plans/review_evidence_store.py`.
- 4.1.7 - The plan skill's unattended rounds invoke `coordinator_decision`. test: `tests/skills/test_plan_review_skill.py::test_unattended_round_uses_coordinator_decision`.

### 4.2 Enhancer discriminator and honest test names [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/plan-enhance/SKILL.md`
- `tests/skills/test_plan_enhance_skill.py`
- `src/gobby/install/bundled_content_manifest.json`

The #19287 fix left two adjacent Hard Boundaries contradicting each other (`plan-enhance/SKILL.md:69-77` vs `:114-116`): "correctness defect → point, don't fix" against "conformance drift → propose the fix," with nothing distinguishing the two.

- Update the skill with the discriminator: a *correctness defect* is a wrong claim inside the plan; a *conformance gap* is plan silence about a mechanism a cited contract already fixes. Include the worked E4 example inline.
- Rename `test_mandated_mechanism_survives_suggestion` to state what it asserts (the instruction exists, not that any mechanism survives) — the mechanical detector for survival is 4.1's fold-in validation.
- Regenerate the bundled-content manifest hash for the changed skill.

**Acceptance:**

- 4.2.1 - The discriminator and worked example are in the skill. file: `src/gobby/install/shared/skills/plan-enhance/SKILL.md`.
- 4.2.2 - Prose-contract tests are named for what they pin. test: `tests/skills/test_plan_enhance_skill.py`.

## P5: Enforcement Reachability
`kind: framing`

**Goal**: Every block reason reaches the agent whole and names the action that unblocks it; retry loops terminate deterministically; equivalent write routes gate identically.

### 5.1 Attribution release: reachable, safe, whole [category: code]
`kind: deliverable`

Targets:
- `src/gobby/adapters/claude_code.py`
- `src/gobby/workflows/commit_guard.py`
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_paths.py`
- `src/gobby/workflows/state_manager.py`
- `src/gobby/install/shared/workflows/rules/task-enforcement/require-commit-before-status.yaml`
- `src/gobby/install/shared/skills/tasks/SKILL.md`
- `tests/adapters/test_claude_code_adapter.py`
- `tests/workflows/test_commit_guard.py`

The #19306 release instruction is truncated out of the block reason on Claude: the adapter compacts deny reasons to `_DENY_REASON_MAX_CHARS`, cutting `release_task_paths(...)` mid-token with one conflict and dropping it entirely with two. Current Claude Code hooks documentation specifies a 10,000-character context-injection limit with overflow saved and referenced, and documents no 300-character deny-reason limit — the local compaction is unnecessary.

- Remove the local deny-reason compaction (`claude_code.py:112,139-144`). Test the full agent-visible reason above 300 characters, and the provider's referenced-overflow behavior above 10,000.
- Update `require-commit-before-status.yaml` (lines 21-24) so the owner-side reason names the `release_task_paths` alternative for foreign-attributed dirt; add the same to the tasks skill's Completion Gates.
- Modify `release_task_paths` to refuse paths with uncommitted content: `git --literal-pathspecs status --porcelain -z -- <paths>` (literal pathspecs so names beginning with pathspec magic like `:(...)` cannot be misread; pattern adapted from `commit_guard.py:236-251`), returning the dirty list. Test colon- and glob-significant filenames.
- Fix the minors: `_format_ref`'s UUID-prefix fallback emits an unresolvable `task_id` (emit the full UUID); unify the error envelopes in `_lifecycle_paths.py:63-72` on `task_error`; normalize `remaining` in `state_manager.py` release results.
- Add an end-to-end test driving the real MCP tool through the guard (both existing tests bypass half the chain), and an adapter corpus case for the template-generated reason.

**Acceptance:**

- 5.1.1 - The full block reason, including the release call, reaches the agent unmodified. test: `tests/adapters/test_claude_code_adapter.py::test_deny_reason_not_compacted`.
- 5.1.2 - Release refuses paths with uncommitted content, using literal pathspecs. test: `tests/workflows/test_commit_guard.py::test_release_refuses_dirty_paths`.
- 5.1.3 - The owner-side rule reason and tasks skill name the release path. file: `src/gobby/install/shared/skills/tasks/SKILL.md`.

### 5.2 Deterministic retry-loop termination [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/enforcement_checks.py`
- `src/gobby/install/shared/workflows/rules/error-recovery/inject-tool-error-recovery.yaml`
- `src/gobby/install/shared/skills/bash/references/quoting-and-data.md`
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`
- `tests/workflows/test_step_enforcement.py`

The #19285 "stop" is prose appended to the same feedback the agent ignored for 2708 seconds — no counter, no termination. F19 (zsh parse errors from unquoted Tailwind syntax) was silently dropped from a task that covered both findings.

- Implement denial counting keyed by (agent run, workflow instance, step entry/revision, rule, normalized target) — session-scoped keys cross runs and rounds. Reset the counter on step transition or successful unblock. The third identical denial persistently transitions the autonomous run to a terminal blocked state and ends that run; it never advances past the guarded step. No "park if no transition applies" fork — termination is the single outcome.
- Modify the reserved-variable branches (`enforcement_checks.py:200-204`, `:342-346`) to variable-specific wording (the current text tells the agent to abandon `set_variable` for the whole step); give the agent-level block branches (`:116-121`, `:169-175`) the same guidance and counting.
- Move the recovery guidance into the deny reason itself: a Claude PreToolUse deny produces no PostToolUse, so the `after_tool` recovery rule never fires for Claude-hosted agents; the rule remains for genuine tool-error events. Test on the final decision surface.
- Recover F19: update the bash skill's quoting reference with zsh `@`-glob guidance (single-quote CSS/Tailwind payloads: `@theme`, `@custom-variant`, parenthesized globs, `#`), and update the adversary workflow's terminal-command guidance to require it.

**Acceptance:**

- 5.2.1 - The third identical denial terminates the run in a blocked state without advancing the step. test: `tests/workflows/test_step_enforcement.py::test_third_denial_terminates_run`.
- 5.2.2 - Counters key on run/instance/step-revision/rule/target and reset on transition or unblock. symbol: `enforcement_checks`.
- 5.2.3 - zsh quoting guidance ships in the bash skill and adversary workflow. file: `src/gobby/install/shared/skills/bash/references/quoting-and-data.md`.

### 5.3 Uniform write-route gating [category: code]
`kind: deliverable`

Targets:
- `src/gobby/hooks/_normalization_canonical.py`
- `src/gobby/workflows/enforcement/blocking.py`
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `tests/workflows/test_task_enforcement_rules.py`

`git apply`, `patch`, and inline interpreters fall through to `execute` classification (`_normalization_canonical.py:552-554,652`), so `require-task-before-edit` never fires for them — the criterion "same target, same decision regardless of tool" holds only for the routes the existing tests exercise. Leaving interpreters open contradicts both the criterion and the nothing-deferred posture.

- Classify `git apply` and `patch` as writes with unknown paths unless a mechanically provable dry-run flag is present (`--check`/`--stat`/`--numstat` for git apply; `--dry-run` for patch) — those flow to the existing fail-closed branch (`blocking.py:435-437`).
- Classify inline interpreter invocations as write-capable unknown operations that fail closed when no task is claimed — the exact set: `python`/`python3` with `-c` (including through `uv run`), `node` with `-e`/`--eval`, `ruby` with `-e`. Arbitrary inline source admits no sound read-only proof, so no read-only carve-out exists for interpreters; the only preserved exceptions in this deliverable are the enumerated dry-run flags above.
- Update the plan skill's exemption line to state the implemented scope (any `.md` under `.gobby/`, `.claude/`, `.codex/` — CLI-owned artifact trees), matching `blocking.py:146-153`; the code scope stays as is.
- Tests on the final normalized decision surface across the command families, including the preserved dry-run forms and a taskless inline-interpreter denial.

**Acceptance:**

- 5.3.1 - Patch-application and inline-interpreter routes gate fail-closed without a claimed task; dry-run forms pass. test: `tests/workflows/test_task_enforcement_rules.py::TestWriteRouteParity`.
- 5.3.2 - The documented exemption scope matches the code. file: `src/gobby/install/shared/skills/plan/SKILL.md`.

## P6: Continuation, Payloads, Records
`kind: framing`

**Goal**: Compaction continuation is normalized and complete; error payloads survive whole; the workflow loader stops parsing rule rows; the task record is reconciled.

### 6.1 Error-payload preservation [category: code]
`kind: deliverable`

Targets:
- `src/gobby/hooks/tool_error_tracker.py`
- `src/gobby/sessions/summary_formatting.py`
- `src/gobby/sessions/summary_transcripts.py`
- `tests/hooks/test_tool_error_tracker.py`
- `tests/sessions/test_summary_generation.py`

F5's artifact (`...activity/CronT`) came from the 300-character bare slices in `tool_error_tracker.py` (lines 297 and 344), not the 24KB transcript cap the #19292 commit marked. No-truncation policy applied at the source:

- Remove the `MAX_ERROR_CHARS` slicing; store the full sanitized error payload in the open-tool-errors record, stamped with a stable error ID.
- Modify the preview surfaces (`summary_formatting.py:101,120`) to full-content storage with a bounded inline preview that names the exact retrieval call — `get_variable(name="open_tool_errors", session_id=<current>)`, then select the record by its stable `error_id` — never an abstract pointer.
- Modify the 24KB transcript-fallback overflow to reference the stored full content, naming `get_handoff_context` (gobby-sessions) as its retrieval call (existing handoff pattern: inline head plus explicit call), and fix the marker-less `max_chars <= 16` band in the truncation helper.
- Test: a 900-character multi-path validator error — full text retrievable by error ID; the injected preview names the retrieval operation.

**Acceptance:**

- 6.1.1 - Error payloads are stored whole with stable IDs; previews name the retrieval operation. test: `tests/hooks/test_tool_error_tracker.py::test_full_payload_stored_with_retrieval_id`.
- 6.1.2 - Long validator errors round-trip whole to the handoff surface. test: `tests/sessions/test_summary_generation.py::test_long_error_referenced_not_cut`.

### 6.2 Compact continuation: actor naming and reload normalization [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/sessions/compact_continuation.py`
- `src/gobby/mcp_proxy/tools/sessions/_terminal.py`
- `src/gobby/install/shared/skills/goal/SKILL.md`
- `src/gobby/install/shared/skills/build-coordinator/SKILL.md`
- `src/gobby/install/shared/skills/bridge/SKILL.md`
- `src/gobby/install/shared/skills/plan/SKILL.md`
- `src/gobby/install/shared/workflows/rules/context-handoff/auto-compact-after-task-close.yaml`
- `src/gobby/install/shared/workflows/rules/skill-discovery/reset-skill-injection.yaml`
- `tests/sessions/test_compact_continuation.py`
- `tests/skills/test_plan_skill_delegated_mode.py`

The daemon-as-actor interrupt warning landed on 2 of 7 surfaces; the reload directive repeats the full get_skill instruction once per skill (9 times in the user's live continuation prompt) and mandates the entire ledger regardless of remaining work — a session that has finished coding and tests, with only commit-and-close left, is told to reload `python`; `workflow_requested_skills` grows monotonically across compactions.

- Extract one canonical daemon-as-actor string; update the remaining surfaces: the three skills above, the auto-compact rule's wording, `_COMPACT_SELF_CONTINUE_INTRO` (the surface the agent actually reads post-interrupt), and the plan skill's enhancer call site (line 125). Parametrized skill-contract test across the four skills.
- Modify the compact-resume reload directive (`_collect_compact_resume_required_skills` and its prompt renderer) to emit the get_skill instruction once, followed by a two-tier name list. **Required tier**: `required_skills`, `claimed_task_required_skills`, and the whole `workflow_requested_skills` ledger — a flat `list[str]` with no workflow or step identity, so nothing finer than the whole list is reconstructible — listed as must-load. The rules engine remains the backstop for this tier (a skipped required skill bounces organically on first gated tool use); the prompt listing just saves the bounce. **Advisory tier**: the remainder — `additional_skills`, `claimed_task_additional_skills`, and the residual `loaded_skills` ledger, deduplicated against the required tier — presented as "reload any still relevant to your remaining work," agent judgment, never a mandate. The ledger-clear on compaction stays; the prompt snapshots both tiers before the reset fires. The tiers flow through `compact_self`'s existing surface — `persist_compact_resume_required_skills` and the `compact_resume_required_skills` result field (`_terminal.py:574,638-639`) — which is updated to carry both. Reload stays agent-driven via `get_skill`; skills are never auto-loaded through additionalContext (Claude caps additionalContext at ~10k — skill bodies would blow it). Update the module comment at `compact_continuation.py:50-53` ("the ledger is part of the resume set" becomes "the ledger is the advisory tier") and the gobby-memory record `0d6e4c35` carrying the same superseded framing.
- Update `reset-skill-injection.yaml` to clear `workflow_requested_skills` alongside `loaded_skills` so both ledgers describe the current context cycle.
- Integration test spanning load_skill effect → persisted variable → continuation prompt.

**Acceptance:**

- 6.2.1 - All seven surfaces carry the canonical actor warning. test: `tests/skills/test_plan_skill_delegated_mode.py`.
- 6.2.2 - The continuation prompt gives one instruction plus a tiered name list: required (rule-enforced) then advisory (agent judgment). test: `tests/sessions/test_compact_continuation.py::test_reload_directive_normalized`.
- 6.2.3 - Both skill ledgers reset on context-cycle start. file: `src/gobby/install/shared/workflows/rules/skill-discovery/reset-skill-injection.yaml`.
- 6.2.4 - Memory `0d6e4c35` and the module comment at `compact_continuation.py:50-53` are updated to the tiered framing. behavior: "memory 0d6e4c35 tier update" in `.gobby/plans/epic-19276-review-fixes.md`.

### 6.3 Record reconciliation and stale pressure band [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/observer_context_usage.py`
- `tests/workflows/test_context_handoff_rules.py`

#19216 and #19309 are both closed-completed with mutually exclusive criteria; `3a0d5d8fb` reverts `a8354babf` line-for-line without mentioning it. The plan-mode contract is user-confirmed: no context-pressure nudges in plan mode, ever; the `is_spawned_agent` conditional is reserved for future autonomous plan-to-PR. Code direction stands.

- Annotate #19216 (via gobby-tasks) as superseded by #19309, recording the contract so it is not re-litigated. #19276 itself is reopened as this epic's root, so its unmet adversary-respawn acceptance stays home and P7 discharges it.
- Fix the stale-band bug: the plan-mode early return leaves `context_compact_mid_turn_pressure_band` stale (`observer_context_usage.py:110` returns before line 121 writes), suppressing a legitimate re-nudge after plan exit until pressure exceeds the old band. Reset the band and shown-kinds on the plan-mode path.
- Test: enter plan mode at strong pressure, exit, and assert a fresh nudge fires.

**Acceptance:**

- 6.3.1 - Plan-mode entry resets pressure-band state; post-exit nudges fire fresh. test: `tests/workflows/test_context_handoff_rules.py::test_plan_mode_resets_pressure_band`.
- 6.3.2 - The superseded-record annotations exist on #19216 and reopened #19276. behavior: "record reconciliation note" in `.gobby/plans/epic-19276-review-fixes.md`.

### 6.4 Workflow loader stops parsing non-workflow rows [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/loader.py`
- `tests/workflows/test_loader.py`
- `tests/mcp_proxy/tools/workflows/test_get_workflow_not_found.py`

Live daemon ERROR, diagnosed against the DB before fan-out: `workflow_definitions` holds five row types (178 rule, 42 variable, 29 workflow, 29 agent, 11 pipeline), and `_load_from_db` (`loader.py:145-146`) type-guards only `agent` — a by-name lookup that lands on the rule row `require-claimed-task-required-skills` (`workflow_type='rule'`, definition carrying `when`/`event`/`effects` and no `name`) force-parses it as a `WorkflowDefinition` and throws. The serialization contract is sound; the loader's type filter is the single fix site.

- Modify `_load_from_db` to return None for every non-loadable `workflow_type` (`rule`, `variable`, alongside the existing `agent` guard), logging at debug with the requested name and row type so the upstream caller that requests a rule name as a workflow is identifiable from the log.
- The caller is already identified: the `get_workflow` MCP tool (`src/gobby/mcp_proxy/tools/workflows/__init__.py:155-161`) is the by-name lookup surface invoked with a rule name. With the loader guard in place, `get_workflow` returns its clean not-found response — proven with a test on the MCP tool surface, not only the loader unit.
- Test: a rule row and a variable row resolve to None without raising; workflow and pipeline rows parse as before.

**Acceptance:**

- 6.4.1 - Non-workflow row types resolve to None instead of raising. test: `tests/workflows/test_loader.py::test_loader_skips_rule_and_variable_rows`.
- 6.4.2 - The parse-failure log signature is gone after restart. behavior: "loader triage note" in `.gobby/plans/epic-19276-review-fixes.md`.
- 6.4.3 - `get_workflow` on a rule name returns a clean not-found at the MCP tool surface. test: `tests/mcp_proxy/tools/workflows/test_get_workflow_not_found.py`.

## P7: Integration and Adversary Validation
`kind: framing`

**Goal**: The repaired machinery is proven live: quiet logs across restarts, and the web-styling plan survives two unattended adversarial review rounds with every finding cleared in-session.

### 7.1 Post-restart log monitoring and triage [category: test] (depends: P1)
`kind: deliverable`

Target: `.gobby/plans/epic-19276-review-fixes.md`

After every daemon restart in this epic (at least the P1 bootstrap gate and any mid-epic restarts):

- Stand up a log monitor on `~/.gobby/logs/` immediately after the restart (background watcher surfacing new ERROR/WARNING lines and tracebacks).
- Every error, warning, or failure it surfaces is fixed in-session — caused by this epic or pre-existing. Diagnostics confined to a subagent-owned dirty path route to that owner via P2P while it is live; the coordinator retains accountability and fixes anything unowned.
- The monitor stays up through both adversary rounds; the epic's close bar includes a quiet log — no unexplained ERROR/WARNING — at round-2 close. Triage results are recorded in this plan's changelog.

**Acceptance:**

- 7.1.1 - Per-restart triage entries exist and the final sweep is quiet. behavior: "post-restart log triage record" in `.gobby/plans/epic-19276-review-fixes.md`.

### 7.2 Final integration and two unattended adversary rounds [category: test] (depends: P1, P2, P3, P4, P5, P6)
`kind: deliverable`

Targets:
- `.gobby/plans/epic-19276-review-fixes.md`
- `.gobby/plans/web-styling-consolidation-phase-2.md`

Runs after every implementation phase (P1–P6) is closed; this deliverable stays open through finding repair — it is the task/commit boundary for in-session fixes.

1. Final coordinated reinstall/sync/restart validation (the P1 bootstrap gate already ran once; re-run install/sync if any bundled YAML changed after it).
2. `expire_plan_review_evidence` on the still-bound evidence `e7fd18eb-5d84-4d8e-bbe1-d42aecb7279b`.
3. Re-submit `.gobby/plans/web-styling-consolidation-phase-2.md` through the review pipeline: two full adversarial review rounds, unattended, per the plan skill's round protocol (prepare → spawn → bind_evidence_run → compact_self → wait → manifest/checkpoint/finalize). Reviewer model comes from the adversary agent definition; pass no override.
4. Findings are judged by the coordinator through the 4.1 `coordinator_decision` provenance path — no user votes. Every finding from both rounds is fixed in-session under this task; nothing is filed for later, nits included. `compact_self` between rounds.
5. Proof points: no livelock or timeout (F12/F18 fixed); findings round-trip through the transactional vote gate (4.1 live); full deny reasons visible in the adversary transcript (5.1 live); quiet logs (7.1).
6. Stop after round 2. Report state to the user, who decides whether to run more rounds. The reviewed plan is not expanded and not declared done.

**Acceptance:**

- 7.2.1 - Two finalized unattended rounds with checkpoints exist in the reviewed plan's changelog. behavior: "Round 1 and Round 2 checkpoints" in `.gobby/plans/web-styling-consolidation-phase-2.md`.
- 7.2.2 - Coordinator-provenance vote artifacts back both rounds' decisions. behavior: "coordinator-authored vote artifacts" in `.gobby/plans/web-styling-consolidation-phase-2.md`.
- 7.2.3 - The end-state report (proof points, quiet log, stop-for-user) is recorded. behavior: "round-2 close report" in `.gobby/plans/epic-19276-review-fixes.md`.

## V2 End-to-End Verification
`kind: verification`

- Per-child validation: focused `GOBBY_TEST_PROTECT=1 uv run pytest <touched test paths> -q` (never the full suite), `uv run ruff check src/`, `uv run mypy src/`; `cargo test -p gobby-core -p gobby-hooks` where crates change. Pytest must run isolated from the user's live daemon.
- The landed heartbeat fix (#19317, commit `c0749939d`) is re-validated by including `tests/workflows/test_pipeline_heartbeat.py` in the final focused sweep — no implementation leaf exists for it.
- Swarm review lifecycle enforced on every child: author live → coordinator precommit diff review → author fixes and commits → author submits for review → coordinator independently validates → approve/reject; coordinator closes only through the review workflow.
- P1 bootstrap gate (1.6) is the swarm-enable switch: install, restart, rotation with old-token failure assertions, live sandbox smoke.
- The epic's close bar: all implementation children closed with linked commits; two finalized unattended adversary rounds on the web-styling plan with every finding repaired in-session; quiet daemon logs; the user decides on further rounds before anything is declared done.

## V1 Plan Changelog
`kind: verification`

<!-- Manual review rounds (Codex rev-1, rev-2, rev-3) applied pre-artifact. Rev-3: 1.6/7.2 dependency-edge closure; evidence-artifact schema targets (migration 350, models/store, plan-skill contract); coordinator_decision auth contract (operator-authenticated, caller session == evidence session_id, agent tokens rejected) with inline output + digest and target_section_id; whole-ledger required tier flowing through _terminal.py plus memory-0d6e4c35 acceptance; exact-set acknowledgement predicate; deterministic token expiry (verified: no existing 1800s run fallback — untimed runs get a 24h ceiling, run liveness is the real gate); 2.3 removed as landed (#19317 c0749939d); enumerated interpreter gating with no read-only carve-out; get_workflow caller resolved with MCP-surface test; concrete retrieval calls (get_variable open_tool_errors / get_handoff_context); migrations 349/350 pre-allocated. Adversarial rounds in this changelog belong to plan-format validation only; the epic's own adversary work (7.2) targets the web-styling plan. -->
