# #22340 working context (W4 gobby#15469): research for `.gobby/plans/gobby-messaging.md`

Session-scoped working notes. The plan file, once written, is the authority.

## Assignment (Orchestrator gobby#14972, 12:37 CT 2026-10-05)

- Pod 4 (W4 gobby#15469 writer, Adv4 gobby#15471 adversary) takes #22340 "Plan
  gobby-messaging: channel-based agent chatboard as a native gdaemon family". Copy
  LM7 gobby#15389. Same council flow as #23275: draft, base-validate, commit, one
  `plan-enhancer-taskless-old` pass (no task_id, isolation none; add label
  `enhancer-pass-spent` by hand if the receipt rule does not fire), Orchestrator
  dispositions, Adv4 consensus by send_message, V1 consensus entry, Adv4 applies M1,
  commit rendered bytes unchanged, send stamped plan to Orchestrator. No expansion
  before Josh approves.
- #23275 (gobby-mcp crate) plan is stamped at e4e60efccd and escalated to the
  Orchestrator awaiting Josh approval. Its D2 (streamable HTTP) defers to #21570.
- Task validation criteria: plan at `.gobby/plans/gobby-messaging.md` covering
  channel/member/message schema with per-member cursors replacing
  inter_session_messages fan-out; wait_for_message via pg_notify; subscription
  levels and mention routing within the 6500-char piggyback budget; wake defaults
  per channel kind; planner/adversary consensus protocol (proposal hash,
  blocking/nit objections, accept) and its relationship to the attestation round;
  RouteFamily crate placement and Stage 2 dependencies; Python glue inventory that
  dies at S2.11/S2.12.

## Primary research input

`docs/research/agent-messaging-chatboard.md` (212 lines, recorded 2026-09-14, commit
efc915265a). Read it in full first; it maps mailbox, wake, hook piggyback, the old
review loop, Rust workspace, roadmap sequencing, proposed model, consensus
protocol, glue, and six open decisions. Its Rust-workspace facts are STALE (see below).

## Current state (verified 2026-10-05)

- Stage 1 #21543 children: #21551 gdaemon serve (axum front door) CLOSED 09-29;
  #21552 HTTP corpus CLOSED; #21553 run modes CLOSED; #22411 gdaemon heartbeat host
  (tokio scheduler, interval registrants) CLOSED 10-05; #21554 singleton lease OPEN;
  #21555 API keys OPEN (contains #23275 gmcp, #23273/#23519 key cutover, #23274 node
  relay).
- RouteFamily seam exists: `crates/gdaemon/src/front_door/routes.rs:26` trait,
  `FAMILIES` static :37 (only `HealthFamily` today). `crates/gterminals` is the
  first family-crate-shaped crate (`crates/gterminals/src/lib.rs:9`).
  Workspace members: gcode, gcore, gclient, gdaemon, ghook, gterminal, gterminals.
  Family crate rules: `crates/AGENTS.md` ~lines 80-104 (crate added to members
  when its epic is claimed; RouteFamily export at :89).
- S2.1 #21557 gcore async Postgres: plan approved; P1 Pool and Budget #23081
  CLOSED 10-01; P2 Transaction Seam #23082 OPEN. Parent is now 22801.
- Stage 2 #21544 open epics: #21558 native WS (unblocked), #21559 config/handshake
  (blocked by #21557), #21560 tasks, #21562 sessions, #21563 memory, #21564
  agents/attention/dispatch, #21566 external-MCP multiplexer, #21567 workflows,
  #21568 remaining, #21569 hook ingress (S2.11), #21570 MCP flip (S2.12).
- Council process already changed the first consumer: Writer and Adversary now
  converse via `gobby-agents:send_message` + `wait_for_coordination(reply=true)`
  (this pod did exactly that for #23275). Re-check the review.md flow and
  plan-adversary agent definitions before claiming the cold-spawn loop is current.
- Memory d883cafa (send_message targets, parent-target trust boundary,
  rule-based spawned-agent send scope) is relevant; fetch it.

## Still to research

1. Current mailbox code vs the 09-14 map (`sessions/mailbox.py`,
   `mcp_proxy/tools/agent_messaging.py`, `get_inter_session_message`,
   `wait_for_coordination` reply mode, coordination_waits migration number,
   pending_messages budget, event_enrichment piggyback).
2. `crates/AGENTS.md` family-crate section and ROADMAP decisions 16-18.
3. `.gobby/plans/gdaemon-front-door.md` and the S2.1 plan for the repository seam
   and transaction API a gmsg crate would use; S2.2 #21558 scope.
4. `review.md` and council role files for the current consumer flow.
5. Exemplar plan structure: `.gobby/plans/gmcp-stdio.md` (this pod's stamped plan).
6. Then call advisor before choosing decisions on the six open questions.

## Orchestrator rulings (gobby#14972, 2026-10-05)

- 12:52: invariants live in the schema; crates/gmsg owns repositories, native
  /api/messaging routes, waits, LISTEN; Python is a table client behind ONE adapter.
  Conditions: (1) adapter keeps the current storage/inter_session_messages.py
  interface (callers unchanged), holds no messaging logic (seq, id, pending, ack
  come from SQL), size <= 471 lines; (2) Python glue inventory names the adapter
  and each caller family with the Stage 2 task that deletes it; deletions are
  kind: deferred sections (plan-coverage.md 321-324); (3) record as a Decision
  naming Josh's "no Python interim build" line so he rules at approval.
- 12:56: (1) wake delivery stays one Python glue seam: the existing listener
  pattern subscribes to the new notify channel; its retirement is a deferred
  section owned by #21564 (S2.7). gmsg owns wait rows, registration, SQL
  resolution trigger, notify channel. (2) Re-parent the implementation epic
  under #21544 with an epic-level blocked-by on #21559 (S2.3), S2.8 A2
  precedent; real edge created at expansion, recorded in the Decision Record.
  No pool wiring in this plan. New tool registrations forward ONLY to
  /api/messaging; their deletion is a deferred section owned by #21570.
  MailboxService.send global/project loop -> one channel post is the accepted
  named exception.
- MM hold for #23535 released 12:53 (d1a78237ea). Commits allowed (index empty first).

## Design (settled, draft from this)

Rung 1: today's mailbox works (council uses send_message + reply waits). Gains:
broadcasts stop being N rows, late joiners/history, threads, channel waits,
typed consensus tied to plan bytes, subscription levels.

Schema (new names avoid comms_*/chat_*): agent_channels(id uuid, kind
global|project|tree|direct, machine_id (global/project), project_id (project),
root_session_id (tree), direct_key "a:b" sorted (direct; a:a = self), next_seq
bigint); agent_channel_members(channel_id, session_id, level all|mentions|mute,
delivered_seq bigint, delivered_at timestamptz; rows created lazily; PK pair);
agent_messages(id uuid client-assignable PK, channel_id, seq UNIQUE(channel,seq),
from_session, thread_root_id, content, priority, message_type, metadata_json
(wake_requested stays here), mentions uuid[], broadcast bool, sent_at);
agent_message_waits(id, waiter_session_id, channel_id, after_seq, from_session?,
thread_root_id?, expires_at, outcome, message_id, completed_at, delivered_at).
- BEFORE INSERT trigger locks channel row, assigns seq=next_seq and
  sent_at=clock_timestamp() => per-channel seq order == commit order, no gaps
  (rollback restores next_seq). Raw INSERT ... ON CONFLICT (id) DO NOTHING still
  works (resume_finalization uuid5 dedupe).
- Membership (Decision; cross-family read lives in schema; #21562 owns columns):
  global = sessions on machine M, source != system; project = + project_id;
  tree = root R + descendants via parent_session_id lineage (clear successors
  included; root = topmost ancestor); direct = the two endpoints. Visibility for
  pending: member, not own post (except self-direct), session.created_at <=
  sent_at for global/project, level all, or mentions-level and (broadcast or
  mentioned); mute sees nothing. Default level: global/project mentions,
  tree/direct all. Machine-scoped like resolve_broadcast_selection.
- SQL functions (both Python adapter and gmsg call them): agent_channel_for(...)
  get-or-create; agent_messages_pending(session) ordered (sent_at, channel_id,
  seq); agent_messages_ack(session, ids[]): per channel new cursor =
  max(cursor, max acked seq < U) where U = min visible pending seq not acked
  (contiguous; concurrent out-of-order ack never skips an unacked message ->
  at-least-once, duplicates only under race); sets delivered_at;
  agent_channel_recipients(channel, sender) live members (replaces Python
  resolve_broadcast_selection query; mailbox_targets.py retires);
  agent_wake_pending_sessions().
- AFTER INSERT trigger: resolve matching agent_message_waits (channel, seq >
  after_seq, optional from/thread) + pg_notify('gobby_agent_message_wait', id);
  coordination keyed-release + status branches retargeted to agent_messages
  (release = direct channel owner->waiter, message_type coordination_release,
  metadata coordination_key); reply branch and coordination_waits.reply column
  RETIRED (wait_for_message(channel=direct, from_session=owner) replaces it;
  consumers: references/agents/messaging.md:30-33, docs/guides/agents.md:325).
- Drop inter_session_messages (4 indexes, 2 FKs, grant). Migrations from 460.
  Carriers per migration (see commit b5a436f0e3): migrations/NNN.sql,
  catalog.manifest.json, gcore/src/schema/assets.rs::MIGRATIONS,
  gcore/src/grant/bundle.rs::expected_schema_identity (+ checksum consts),
  gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity,
  gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract,
  src/gobby/storage/schema_expected_identity.json,
  tests/contracts/http/runtime_handshake.json, tests/runtime_grants/golden/{brokered_datastores,
  direct_datastores,old_client_new_grant,payload_skew_unknown_field,unavailable_datastores}.json.
  baseline.sql is NOT regenerated (baseline@420).

Python adapter (storage/inter_session_messages.py, 471 lines, keep class names):
methods create_message, get_message, get_messages, has_completion_notification,
get_undelivered_messages, has_unread_without_read_since (uses member
delivered_at), get_undelivered_wake_messages/recipients (metadata
wake_requested), mark_delivered_batch (-> ack fn), delete_delivered_before
(never deletes seq > MIN member delivered_seq), list_messages, mark_delivered
(web chat _pending_messages.py:68 -> ack fn single id, contiguous rule). Add
fields channel_id, channel_kind, seq, thread_root_id, mentions to
InterSessionMessage; to_session = reading member for received, peer for direct
sent, "" for sent group rows. Additive kwarg get_message(id, reader_session_id)
so get_inter_session_message's sender/recipient check passes for channel members
(name it with the mailbox exception). create_message(from,to) posts to the direct
channel. Raw-SQL callers retargeted: agents/resume_finalization.py (INSERT),
storage/decision_answers.py (JOIN delivery.id = answer.id; to_session),
workflows/engine/templating.py (_has_pending_messages/_pending_message_count ->
pending fn), runner_maintenance/messaging.py:103 (UPDATE delivered_at).
Reservation (hooks/pending_message_reservations.py 88 lines) unchanged: SQL
contiguous ack is the safety net. receipt_effects.apply_acknowledged_receipt
unchanged (mark_delivered_batch).

gmsg crate (crates/gmsg, package gobby-messaging, publish=false): exports
FAMILY_NAME "messaging", ROUTE_PREFIXES ["/api/messaging"] (gterminals
pattern); repository over gcore pool/transaction seam calling the SQL
functions; routes: channels list, channel history (after_seq paging), post
(thread, mentions uuid list, message_type, metadata, wake flag), subscription
set, waits register (immediate resolve if satisfied), thread consensus.
gdaemon adapter: crates/gdaemon/src/front_door/messaging.rs (RouteFamily impl)
+ routes.rs::FAMILIES; pool from the state #21559 provides. Caller identity:
verified front-door principal (#23273); acting session from X-Gobby-Session-Id;
only operator principal (Python backend shims, operator clients) may act for a
session; agent tokens refused until #21570.

New Python tool shims (logic-free, forward to /api/messaging, on gobby-agents):
read_channel, post_message, set_subscription, wait_for_message (calls existing
headless_wait_refusal), get_consensus; post_message calls existing
dispatch_mailbox_wakes with recipients gmsg returns. Wake defaults (omitted):
direct/tree wake visible members; global/project wake only mentioned members;
wake=true all visible; wake=false none. send_message keeps its targets
(global/project -> one post with broadcast=true; parent/session/agent -> direct
channel; build -> as today per recipient? (build = coordinator direct)).

Wait delivery seam: CoordinationWaitService parameterized (notify channel,
manager) and instantiated for agent_message_waits (new small Python manager over
SQL refresh fn) -> registry.wake_sessions. Retirement deferred to #21564.

Consensus protocol: thread in the project channel (board-visible, mentions
level, mentioned seats woken). message_type proposal (metadata plan_path,
plan_sha256 of committed bytes, commit, version n), objection (proposal_id,
severity blocking|nit, finding_id), accept (plan_sha256). Consensus = the
adversary's latest thread message is accept naming the latest proposal hash and
no blocking objection after that proposal. Proposal versions count rounds
(max_review_rounds -> escalate to coordinator). Relationship: consensus on
narrative hash; Writer's dated V1 entry cites accept message id + hash;
Adversary then derives/applies M1 (attestation survives, review.md flow;
decision 4). Decision 5: PD + Josh after stamping; anyone may read/interject in
the thread; no per-finding votes. Decision 6: no live board; deferred #21558.
Templates: workflows/agents/plan-writer.yaml, plan-adversary.yaml,
references/plan/review.md, references/agents/messaging.md, docs/guides/agents.md.

Leaves (draft): 1.1 additive schema+functions+triggers (mig 460); 1.2 adapter
retarget + raw-SQL callers + mailbox exception + coordination retarget + drop
inter_session_messages (mig 461); 1.3 gmsg crate repository; 1.4 gmsg routes +
gdaemon family registration; 1.5 tool shims + wait delivery seam; 1.6 consensus
protocol in templates/docs. Check sizes: events/wake.py 937, hooks/inbox.py
983, sessions/compact_continuation.py 887 must NOT be targets (adapter keeps
interface). mailbox.py 809 ok.

Deferred sections (owners; all cited-parent:#21544 + out-of-scope-for:#22340 on
#21544, gmcp D2 shape): hook piggyback glue -> #21569; MCP tool registrations
(existing messaging tools + new shims) -> #21570; wake seam, mailbox target
resolution, coordination waits, resume_finalization -> #21564; compact
continuation, clear-successor redirect, cli sessions messages -> #21562; rules
engine queries (templating, blocking, run_command_effects) -> #21567;
_stage_review + tasks_assignment -> #21560; comms + decision answers -> #21583;
web chat glue -> #21592; retention loop -> #21582; live board -> #21558;
adapter deletion -> #21574 (S3.4 retire gobby-backend).

## Facts verified after compaction (13:00-13:20 CT, HEAD d3985d1130)

- Migrations: `crates/gcore/assets/schema/migrations/NNN_name.sql`; latest 459; ours 460
  (additive), 461 (retarget + drop), 462 (reply-wait retirement, in the council leaf).
  Carrier symbols: `assets.rs::MIGRATIONS`, `bundle.rs::GOLDEN_LATEST_CHECKSUM` +
  `bundle.rs::expected_schema_identity`, `schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`,
  `cli_contract.rs::version_json_reports_exact_schema_identity_contract`, plus JSON carriers.
- Old table: id/from_session/to_session uuid, content, priority, sent_at, message_type,
  metadata_json jsonb, delivered_at; FKs to sessions ON DELETE CASCADE DEFERRABLE; 4 indexes;
  GRANT S/I/D/U to gobby_daemon_runtime. sessions: id/machine_id/project_id uuid, source,
  status, parent_session_id uuid, created_at, updated_at.
- Coordination SQL: resolve_coordination_wait (431; 441 adds reply_message_id; 447 live
  identity); trigger coordination_message_committed AFTER INSERT ON inter_session_messages.
- Raw-SQL readers: resume_finalization.py::notify_parent_of_recovery (INSERT ON CONFLICT DO
  NOTHING => seq gaps), session_coordinator.py::SessionCoordinator.complete_agent_run (777
  lines), decision_answers.py::_ON_THIS_MACHINE + ::_close_mailbox_row,
  templating.py::TemplatingMixin._has_pending_messages + ._pending_message_count,
  runner_maintenance/messaging.py::cleanup_zombie_messages_loop.
- MailboxService.send loops create_message; dispatch_mailbox_wakes needs
  len(messages)==len(session_ids). resolve_broadcast_selection: LIVE_SESSION_STATUS_ORDER
  (active, paused, interrupted, awaiting_input, awaiting_approval, awaiting_handoff),
  machine, source != system, id != sender, project; selector_metadata {scope, recipient_states}.
- Wake replay (events/wake_recovery.py) uses get_undelivered_wake_recipients/_messages.
- CoordinationWaitService(manager, registry, machine_id, connection_factory), LISTEN
  hardcoded, built in runner_lifecycle.py::run_daemon. Plan: one service, managers keyed by channel.
- Reply consumers: messaging.md:33, agents.md:325, coordination.py description,
  stop-gates/require-task-close.yaml:16, require-epic-tree-close.yaml:20.
- RouteTable::new defaults families to Proxy; add RouteFamily::default_backend.
- gcore feature postgres-pool; Pool::transaction(lock, f); Rust DB tests use
  GOBBY_SCHEMA_TEST_DATABASE_URL. Python front-door client: utils/daemon_url.daemon_url +
  local_token.daemon_auth_headers.
- cited-parent (plans/deferral.py::_has_valid_cited_parent): X not in root closure, open,
  labeled out-of-scope-for:<root>; no parent relation needed. Root = #22340.
- Titles: #21558 Native WS transport in gdaemon; #21559 Config, runtime handshake, and grant
  issuing; #21560 Tasks family; #21562 Sessions and transcripts; #21564 Attention, agents,
  dispatch, and worktrees with machine scoping; #21567 Workflows, rules, pipelines, build, and
  validation; #21569 Hook ingress and the node-local envelope ledger; #21570 MCP front door
  flip; #21574 Retire gobby-backend; #21582 Cron and scheduler route family; #21583
  Communications route family: Telegram and Slack; #21592 WS chat route family.
- HOLD: MM 15406 #23536; no staging/commit until release (ACK sent).
