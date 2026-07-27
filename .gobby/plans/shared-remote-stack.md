# Shared Remote Stack Roadmap (#17488)

**Plan ID:** shared-remote-stack
**Plan kind:** strategy (roadmap; no manifest, no expansion)
**Root:** epic #17488 — Future planning: shared daemon with machine-local execution

## Overview
`kind: framing`

Josh runs the Gobby Docker stack locally on his laptop; working from another machine
means copying the stack. Goal: **work from anywhere without stopping work when the
laptop packs up** — close machine A mid-task, open machine B, resume the same task with
the same context. Long-term recorded direction (not scope here): monetize Gobby as a
hosted service — users install the daemon locally, Gobby hosts the shared stack. The
roadmap must not foreclose that.

This strategy plan sequences epic #17488's children into gated milestones and adds one
new bridge milestone (M0) so continuity arrives before the full worker architecture.
Epic ordering preserved: #17435 → #17437 → #17436 → #17439 → #17769 → #17440; #17438
(fleet) stays future.

North-star acceptance ladder:

- M0 = resume the WORK anywhere (tasks/memory/session metadata shared).
- M1 = resume the SESSION anywhere (transcript content on hub; cross-machine handoff).
- M2 = single shared daemon with per-machine execution (agents/worktrees correct).
- M3 = multi-user-safe (monetization-shaped identity and auth).
- M4 = production cutover to the hub. M5 = optional public hosting, explicitly gated.

## Constraints
`kind: framing`

- 0.5.0 unshipped: no backward compatibility anywhere.
- Tailscale-first: the tailnet is the trust boundary until M3+TLS (M5 gate).
- OS-neutral protocols and path handling; Windows contracts from the start (M2 onward).
- The hub never executes client-filesystem work, never falls back to another machine.
- Least mechanism: M0 work must be genuine pull-forward of #17437, never throwaway.
- Identity contract: `docs/contracts/identity-model.md` — user derivable via
  session → machine → owner_user_id; no scattered user_id columns.

## 1 M0 — Bridge: shared datastores, per-machine daemons (new epic)
`kind: framing`

Topology: the AMD Linux box runs ONLY the Docker datastores (PostgreSQL :60891,
Qdrant :6333, FalkorDB :16379) exposed on the tailnet. Each client machine runs a full
local daemon pointed at the shared datastores. Execution, hooks, and transcripts stay
machine-local because each machine has its own daemon. One-time per client: copy
`~/.gobby/.secret_kek` + `~/.gobby/local_cli_token` from the hub install (shared
wrapped-DEK and token hash; superseded by #17769).

Implementation plan: `.gobby/plans/m0-shared-datastores-bridge.md` (drafted alongside
this roadmap). Highlights: `datastore_mode: local|remote` bootstrap key; compose
bind-address knob + `gobby datastores expose`; migration adding `machine_id` to
worktrees/clones/agent_runs/cron_runs with scoped readers (lifecycle monitor, stale
cleanup, cron reconcile, background transcript consumers); migration lockstep guard;
shared-stack runbook rewrite.

**Gate M0:** pack-up scenario passes for tasks/memory (create/claim on A, resume and
complete on B, converge on A's return); B never marks A's agents dead, never touches
A's worktrees/clones, never fails A's cron runs; version-skew startup fails clean;
datastore ports unreachable off-ACL. This gate doubles as the dogfood cutover for
daily work.

M0 boundaries: code syncs through GitHub (push before packing up); checkout paths
should match across machines until M1/M2; transcripts follow at M1.

## 2 M1 — Remote sessions (#17435)
`kind: framing`

Transcript content reaches the hub so sessions survive machine switches, and a hub
daemon becomes viable as the session plane. Verified design (2026-07-26 session):

- Dedicated `POST /api/transcripts/increments` + `GET /api/transcripts/state` +
  `GET /api/daemon/identity`; hook envelope v1 stays frozen.
- ghook ships deltas inline per hook (bounded) with a detached `ghook ship-transcript`
  drain; client offset-state file; the provider transcript file is the replay log.
- Ordering: `(generation, byte_offset)` + chain hash; self-healing divergence recovery;
  fsync-before-ack replica at `~/.gobby/transcripts/{machine_id}/{session_uuid}.jsonl`.
- `sessions.transcript_path` stays the single canonical column (replica for remote,
  provider file for local; no self-upload; one consumer pipeline). New diagnostic
  column `origin_transcript_path` the hub never opens.
- `project_id` (committed `.gobby/project.json` UUID) required in the increment body —
  this is what makes two machines with different checkout paths resolve to one project.
- Early-M1 fixes that are dormant in M0 but live the moment any ghook targets a
  non-local daemon: machine_id misattribution fallbacks, foreign-cwd project
  re-derivation, restore-endpoint path writes, ghook client-inbox replay drain.

**Gate M1:** two machines, different checkout paths → one project, distinct
machine_ids; duplicate/interrupted/truncated/replaced streams converge to one correct
hub replica; hub never opens `origin_path`; local sessions unchanged; cross-machine
session handoff reads full transcript context from the replica.

## 3 M2 — Local execution (#17437 remainder → #17436)
`kind: framing`

#17437 remainder: machine-scoped operations beyond M0's records slice — reachability
and filesystem validation only on the owning machine, typed ownership errors for
cross-machine references, per-machine project paths replacing single
`projects.repo_path`.

#17436: authenticated outbound client worker (machine_id, platform, capabilities,
heartbeat, session bindings); spawns/git/worktrees/merges/cleanup/capture routed to the
originating session's machine; worker unavailable = typed offline error. Terminal
backend contract extracted from tmux (seam exists at `agents/spawners/base.py`; tmux
currently leaks into `agent_runs` schema and SpawnResult); Windows ConPTY backend
behind the same contract; `SpawnRequest.machine_id` becomes a routing key. Git
operations funnel through two runners plus four ad-hoc helpers — the injectable
transport seam. Worker channel is a new namespace, separate from the browser-facing
WS protocol.

**Gate M2:** MacBook-originated spawn creates its process/checkout/worktree on the
MacBook while the daemon runs on the hub; switching machines uses the new machine's
local resources with shared state; worker disconnect is an explicit failure with zero
hub-local or cross-machine fallback; Windows contract tests pass in CI.

## 4 M3 — Identity and auth (#17439 → #17769)
`kind: framing`

#17439: daemon users; `machines.owner_user_id` becomes a real FK; one user owns many
machines; cross-user machine claims rejected; standalone mode keeps zero user model.

#17769: `user_api_keys` (hashed, overlapping rotation, revocation); every client sends
`Authorization: Bearer <api_key>` + `X-Gobby-Machine-Id`; valid user key auto-enrolls
unknown machines under that user; cross-user access 403; user-scoped secrets (stable
user DEK wrapped by installation KEK; API keys never derive encryption keys).

This is the monetization seam: a hosted control plane is shared-daemon mode with
per-user keys. No SaaS work happens here; the shapes just have to be right.

**Gate M3:** epic acceptance verbatim — two users cannot authenticate as, enroll, or
access each other's machines or secrets; rotation overlaps old/new keys; standalone
gcode/gwiki behavior unchanged.

## 5 M4 — Ops and production cutover (#17440 + #17488 deployment gate)
`kind: framing`

Remote-aware `gobby status`/`gobby health` (today PID-file + localhost only; Rust CLIs
already resolve daemon_url — reuse their diagnostics surface). Actionable states: hub
unreachable, auth failed, worker missing, worker disconnected, capability unavailable,
healthy.

Then the #17488 deployment gate verbatim: isolated end-to-end validation of transcript
replication, project resolution, local-only execution, machine-scoped worktrees, worker
disconnect behavior, auth/ownership, and Windows compatibility contracts — only then
does the AMD hub become the production coding stack (single shared daemon; per-machine
daemons retire to workers).

## 6 M5 — Public hosting (optional, explicitly gated)
`kind: framing`

Railway or any public-internet hub requires: M3 complete, TLS everywhere, and a
hardening review (Qdrant auth, `_PUBLIC_PATHS` audit — several `/api/sessions/*` routes
are unauthenticated even on remote binds today). Until that gate, Tailscale is the
trust boundary. #17438 fleet management remains future planning. Hosted-SaaS remains a
recorded direction, not scope.

## 7 Program verification
`kind: verification`

Each milestone's gate maps to acceptance text already recorded on the epics — no
invented criteria. The M0 and M1 two-machine end-to-end checklists are the real-world
proof runs; the M0 gate doubles as the daily-work dogfood cutover. Milestone plans go
through the standard `/gobby plan` flow (enhancement, adversarial review, manifest on
approval, `gobby build`) when picked up; this roadmap itself is a strategy artifact and
is not expanded.
