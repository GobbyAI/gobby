# Agent messaging chatboard — research for #22340

Recorded 2026-09-14 from session #13287 on branch `0.5.0`. Planning task: #22340
(`needs-planning`). Nothing here is decided; the Decision Record belongs to the plan.

## Intent

An improved agent messaging system: a chatboard with a global channel, a project
channel, private channels for a parent and its subagents, direct messages, and
threads. First consumer: a planning agent and an adversary refining a plan by
back-and-forth messages until consensus, instead of the current loop of edit plan,
snapshot, spawn reviewer, wait for `end_agent_run`, repeat. The daemon wake stays as
the idle-session nudge. Chat lands in context asynchronously without the agent
stopping to read a mailbox.

Placement decision already taken by the user: a Rust family crate
(`crates/gmsg`, package `gobby-messaging`, roadmap decision 16) landing right behind
the gdaemon front door (epic #21543). No interim Python build.

## What exists today

### Mailbox and delivery

- Tools live on the `gobby-agents` MCP server in
  `src/gobby/mcp_proxy/tools/agent_messaging.py` (`send_message` :136-339,
  `get_inter_session_message` :350, `get_inter_session_messages` :387). Targets are
  `global | project | parent | session | agent | build`; `wake` is a transient send
  argument, not stored.
- Business logic: `src/gobby/sessions/mailbox.py` (`MailboxService.send` :129,
  `resolve_target` :229, `_wake`/`_wake_many` :817-905). `global` and `project` are
  fan-out at send time into N direct rows; scope survives only in `metadata_json`
  (`broadcast_id`). Late joiners never see a broadcast.
- Storage: `inter_session_messages` in
  `crates/gcore/assets/schema/baseline.sql:2623-2635` (from, to, content, priority,
  message_type, metadata_json, delivered_at). Python manager
  `src/gobby/storage/inter_session_messages.py`. No channel, thread, `reply_to`, or
  cursor columns anywhere.
- Spawned agents (`agent_depth > 0`) may only send to `parent`
  (`agent_messaging.py:213-231`). A parent send with `message_type="plan_review"`
  auto-writes `agent_runs.result` and latches `review_complete`.
- Wait primitives: `wait_for_agent` and `wait_for_output`
  (`src/gobby/mcp_proxy/tools/agents_query_tools.py:370, :542`),
  `wait_for_coordination` (`src/gobby/mcp_proxy/tools/coordination.py:15`). There is
  no `wait_for_message`. Wait ceiling policy: `src/gobby/mcp_proxy/wait_tools.py:11-21`
  (300 s).
- Push exists only for coordination waits: migration
  `crates/gcore/assets/schema/migrations/431_add_coordination_waits.sql` adds a
  trigger on `inter_session_messages` that resolves waits and fires
  `pg_notify('gobby_coordination_wait', ...)`; the Python listener is
  `src/gobby/events/coordination_waits.py`. Ordinary messages have no LISTEN/NOTIFY.

### Wake

- `src/gobby/events/wake.py`: `CONTINUE_WAKE_MESSAGE` (:33) is the only text ever
  typed into a pane. `dispatch_live_wake` (:201) skips `active` sessions unless
  urgent (:266-273) and returns `next_call_context`; protected statuses
  (`interrupted`, `awaiting_input`, `awaiting_approval`, `awaiting_handoff`, in
  `src/gobby/storage/sessions/_constants.py:38-40`) queue with no side effect.
  Managed terminal path `_send_managed_terminal_wake` (:560-633); 30 s debounce
  (:724-745) keyed on `turn_count`.

### How text enters an agent's context

- Hook piggyback, not polling. `src/gobby/hooks/event_enrichment.py`:
  `_PIGGYBACK_EVENTS = {AFTER_TOOL, BEFORE_TOOL, BEFORE_AGENT}` (:47-51),
  `_inject_pending_messages` (:165-205) prepends rendered messages to
  `response.context`; IDs are staged (:207-223) and committed only on hook receipt
  ack in `src/gobby/hooks/receipt_effects.py:158-182` (at-least-once).
- Rendering budget: `src/gobby/hooks/pending_messages.py` — 6500-char aggregate,
  2000-char inline, overflow deferred by reference with no notice to either party.
- `crates/ghook` is pure transport: stdin JSON (`dispatch.rs:69-92`), atomic inbox
  envelope then POST to `/api/hooks/execute` (`transport.rs:220, :238`), daemon body
  rendered to `hookSpecificOutput.additionalContext` per CLI dialect
  (`action.rs:160, :568`). Request/response only; it fires when the CLI fires a hook.
- Consequence: no CLI offers any other way to put text into a live turn. A busy
  agent receives messages every few seconds via PreToolUse/PostToolUse; an idle
  agent needs the wake. A chatboard changes the model, membership, and protocol,
  not the physics of delivery.

### Plan adversarial review loop (the first consumer)

Reference: `src/gobby/install/shared/skills/gobby/references/plan/review.md`.
Reviewer agent: `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml`
(`provider: codex`, `model: gpt-5.6-sol`, `reasoning_effort: xhigh`,
`isolation: none`). Evidence tools:
`src/gobby/mcp_proxy/tools/plans/review_evidence.py` (`prepare_plan_review_round`
:54, `get_plan_review_snapshot` :115, `bind_evidence_run` :143). Autonomous variant:
`src/gobby/dispatch/spawn.py:96-136`, round count incremented only by
`reject_review` in `src/gobby/mcp_proxy/tools/tasks/_stage_review.py:555`.

One interactive round:

1. Planner base-validates the plan bytes on disk.
2. `prepare_plan_review_round` snapshots the file into the DB, returns `evidence_id`.
3. Planner spawns `plan-adversary-taskless`, binds the run, saves a
   `clear_session=false` handoff, then `wait_for_agent`.
4. Reviewer cold-starts, is forced through three gated skill loads, reads the
   immutable snapshot, spawns three internal lane subagents, dedupes, derives and
   validates the coverage manifest, and sends one canonical `plan_review` JSON to the
   parent. That message type latches completion; any other chatter on it ends the run.
5. `end_agent_run`; parent wakes; human votes per finding; planner edits; round N+1
   restarts at step 1 with zero carried context.

Where the cost is: the cold spawn at xhigh, the three-subagent fan-out, the
whole-artifact snapshot, the single-payload-per-round contract, and human vote
serialization. The message transport is a rounding error.

Rules a chat loop must renegotiate: the immutable-snapshot rule (`review.md:7`),
parent-only sends (`agent_messaging.py:213-231`), the `plan_review` latch, and round
counting tied to a lifecycle transition rather than a message exchange. The coverage
attestation and repair-class flow (`docs/contracts/plan-coverage.md:562-646`) depend
on immutable bytes and should survive.

### Rust workspace

Manifest is the root `Cargo.toml` (`members` :2). Conventions in `crates/AGENTS.md`;
lines 82-104 define the family-crate pattern: directory `crates/g<family>`, package
`gobby-<family>`, `publish = false`, added to `members` when its epic starts, one
static `RouteFamily` (prefixes, `axum::Router` over shared daemon state, service
trait) composed by `gdaemon`.

- `gdaemon` today is the schema-authority CLI only (`crates/gdaemon/src/main.rs:29-53`).
  No tokio, axum, or async Postgres. Front-door leaf 1.2 in
  `.gobby/plans/gdaemon-front-door.md` introduces tokio and hyper for `serve`; WS is
  spliced byte-for-byte in Stage 1, not terminated.
- `gcore` Postgres is the sync `postgres` crate behind a feature. `tokio-postgres`,
  `tokio-tungstenite`, and `hyper` are already in `Cargo.lock`; `axum`, `sqlx`, and
  `rmcp` are not. No LISTEN/NOTIFY in Rust.
- Daemon URL and bearer helpers: `crates/gcore/src/daemon_url.rs:31, :44`,
  `crates/gcore/src/local_token.rs`. Crates always dial loopback so hub and node
  modes hold (front-door decision 17).
- `gterm` owns PTYs and lease-gated writes (`crates/gterminal/src/host/write.rs:39,
  :95`); `gclient` `send_keys` (`crates/gclient/src/app/mod.rs:472`) rides the
  terminal WS `terminal_input` message handled in
  `src/gobby/servers/websocket/terminal_ws.py:424`.
- Python broadcast is in-process fan-out over the WS registry
  (`src/gobby/servers/websocket/broadcast.py:167`); no SSE.
- The MCP proxy mounts Streamable HTTP servers from bundled templates
  (`src/gobby/mcp_proxy/transports/http.py:44`,
  `src/gobby/install/shared/mcp/templates/*.yaml`). gdaemon could serve the
  messaging tools natively as an HTTP MCP server registered by a template.
- Front-door plan D1 (line 1152) reserves a `gobby-mcp` crate (`gmcp`) for stdio
  and streamable HTTP MCP transports, owned by S2.10 and S2.12.

## Roadmap sequencing (ROADMAP.md)

- Decision 16: every Stage 2 family is a workspace-private crate linked into
  `gdaemon`; the routing-table flip is the rollback mechanism. A standalone `gmsg`
  service talking Postgres directly conflicts with this and with the node model.
- Stage 1 (#21543): front door 1.2 gives gdaemon tokio and the `RouteFamily` seam;
  S1.5 is the HTTP contract corpus every native takeover must pass.
- Stage 2: S2.1 (#21557) async Postgres layer; S2.2 (#21558) native WS transport
  with subscription filter and broadcast envelope; S2.3 (#21559) is the takeover
  template; S2.7 (#21564) agents, attention, dispatch; S2.11 (#21569) hook ingress;
  S2.12 (#21570) MCP front door flip.
- Messaging has no dependency on run modes until hub relay exists; a per-session
  channel cursor is mode-neutral.

## Proposed model (for the plan to confirm or reject)

- `channels`: kind `global | project | tree | direct`; `tree` is one channel per
  spawn root, private to the parent and its descendants. Threads are a
  `thread_root_id` inside any channel.
- `channel_members`: session, joined_at, last-read cursor, subscription level
  `all | mentions | mute`. Late joiners read history. Broadcasts stop being N rows.
  Global defaults to `mentions`, or the 6500-char piggyback budget floods.
- `messages`: channel, monotonic `seq`, sender, content, thread root, typed
  `message_type`, metadata. `inter_session_messages` is replaced, not kept (rule 10).
- `wait_for_message(channel, after_seq, timeout)`: blocking tool call backed by
  `pg_notify`, modeled on `coordination_waits`. The agent's turn never ends; the reply
  arrives as a tool result. This is the real chat primitive for a two-agent debate.
- Wake stays the fixed string. Default on for `direct` and `tree`, off for `global`
  and `project`.
- Hook piggyback keeps delivering unsolicited messages to busy agents.

### Consensus protocol for the review consumer

Planner posts a `proposal` carrying a content hash of the plan bytes. Adversary
replies with `objection` messages tagged `blocking` or `nit` (the only severities
`plan-coverage.md:632-646` recognizes) or an `accept` naming the hash. Consensus is
an `accept` on the latest hash with no open blocking objections. Round count becomes
proposal versions and reuses `max_review_rounds`. Converse to converge, then one
formal snapshot round produces the coverage attestation.

## Unavoidable glue until Stage 2 lands

- MCP tools: until S2.12, agents reach tools through the Python proxy. Two options:
  gdaemon serves `gobby-messaging` as a Streamable HTTP MCP server registered via a
  bundled template (zero Python tool code), or thin Python shims that HTTP-call the
  native routes.
- Hook piggyback: until S2.11, `event_enrichment.py` fetches pending messages. Either
  a small Python fetch-and-ack shim against the native routes, or `ghook` merges a
  native pending fetch into `additionalContext` and acks after emit (at-most-once).
- Wake: the Rust family emits `pg_notify`; the existing Python events bridge pattern
  (`src/gobby/events/coordination_waits.py`) dispatches `dispatch_live_wake`.
- Review workflow: `review.md`, `plan-adversary-taskless.yaml`, and the
  `agent_messaging.py` parent-only and latch rules are template and Python changes
  regardless of where the transport lives.

## Open decisions for the planning pass

1. Crate carries its own `tokio-postgres` pool and LISTEN task, becoming S2.1's seed,
   or blocks on S2.1.
2. Native HTTP MCP serving from gdaemon versus Python tool shims until S2.12.
3. Hook pending fetch: Python enrichment shim versus `ghook` merge with
   ack-after-emit.
4. Attestation round survives (recommended) versus chat consensus is final.
5. Human role during a debate: observe and interject from gclient and web chat with
   votes only on the attested round, vote per finding as today, or fully autonomous
   until consensus or max rounds.
6. Whether messaging is the first consumer of the S2.2 native WS transport (live
   board view in gclient) or ships with hook and wait delivery only.
