Plan artifact: `.gobby/plans/gobby-messaging.md`

# Agent messaging on channels: schema-owned invariants, a `gmsg` route family, and a consensus thread

**Plan ID:** gobby-messaging

## Overview
`kind: framing`

This plan specifies #22340 (Plan gobby-messaging: channel-based agent chatboard
as a native gdaemon family). The research input is
`docs/research/agent-messaging-chatboard.md` (2026-09-14). Its Rust-workspace
facts predate the front door and are superseded here.

**The problem.** The mailbox stores one `inter_session_messages` row per
recipient. A `global` or `project` send writes one copy for every live session
on the machine. A session that starts after a send never sees it, and there are
no threads or channel history. Each copy carries its own `delivered_at`, so
"read" is per row rather than per reader. The plan council now converses over
`send_message` and `wait_for_coordination(reply=true)`, but "consensus" is free
text that nothing can check against the plan bytes.

**When the leaves close:**
- Channels (`global`, `project`, `tree`, `direct`), per-member read cursors,
  subscription levels, mentions, threads, and message waits live in the schema.
  SQL functions and triggers own sequence numbers, membership, visibility,
  acknowledgement, wake rules, and wait resolution (1.1).
- `inter_session_messages` is gone. Its rows move into direct channels. Every
  Python reader goes through one logic-free adapter that keeps today's
  interface. The coordination waits read the new table (1.2).
- A `global` or `project` send writes one post instead of one row per
  recipient (1.3).
- The `gmsg` crate (package `gobby-messaging`) owns the repository and the
  consensus fold (1.4). gdaemon serves it natively as the `messaging` route
  family under `/api/messaging` (1.5).
- Agents gain `read_channel`, `post_message`, `set_subscription`,
  `wait_for_message`, and `get_consensus`. `wait_for_message` is woken through
  `pg_notify`, and the piggyback labels channel posts (1.6).
- The plan council converges on a typed consensus thread tied to the plan's
  SHA-256. Reply-mode coordination waits retire (1.7).

The Python glue that remains is listed in the Python Glue Inventory below.
Deferred sections D1 to D11 delete it, each owned by the Stage 2 task that
absorbs its caller.

## Decision Record
`kind: framing`

Orchestrator rulings (gobby#14972, 2026-10-05):

1. **Invariants live in the schema; `gmsg` owns the native surface** (12:52 CT).
   SQL functions and triggers own sequence numbers, membership, visibility,
   acknowledgement, wake rules, and wait resolution. `crates/gmsg` owns the
   repositories, the native `/api/messaging` routes, wait registration, and the
   notify channel. Python is a table client behind one adapter,
   `src/gobby/storage/inter_session_messages.py`. That adapter keeps its
   current interface, so its callers are unchanged. It holds no messaging
   logic: ids, sequence numbers, pending sets, and acknowledgement come from
   SQL. It stays at or under its current 471 lines. Every remaining Python glue
   site is a `kind: deferred` section owned by the Stage 2 task that deletes it.
2. **Josh's "no Python interim build" line is his to rule on at approval.**
   #22340's description says: "No throwaway Python: the user does not want a
   Python interim build." Ruling 1 reads that line as follows. Python gains no
   messaging logic, only a table client (the adapter), five forwarding tool
   shims, and one wake seam. Every one of them is listed with the task that
   deletes it. The Orchestrator flags this reading to Josh at approval.
3. **Wake delivery stays one Python seam** (12:56 CT). The existing listener
   (`src/gobby/events/coordination_waits.py::CoordinationWaitService`) also
   listens on the new notify channel `gobby_agent_message_wait`. Its
   retirement is D3, owned by #21564 (Attention, agents, dispatch, and
   worktrees with machine scoping). `gmsg` owns the wait rows, registration,
   the SQL resolution trigger, and the notify channel.
4. **Placement and the Stage 2 gate** (12:56 CT). At expansion the
   Orchestrator re-parents #22340 from #21543 (Stage 1 - Rust Front Door)
   under #21544 (Stage 2: strangler absorption of the Python daemon behind the
   front door). It then adds an epic-level blocked-by edge from #22340 onto
   #21559 (Config, runtime handshake, and grant issuing), following the S2.8
   precedent (`.gobby/plans/daemon-side-gterm-adoption-and-terminal-ws.md`,
   A2). #21559 puts the gcore async pool into gdaemon's front-door state. This
   plan wires no pool of its own.
5. **The new tools forward only to `/api/messaging`** (12:56 CT). Their
   deletion is D2, owned by #21570 (MCP front door flip).
6. **One named exception to "callers unchanged"** (12:56 CT).
   `MailboxService.send` replaces its per-recipient loop for `global` and
   `project` with one channel post (1.3). The adapter gains the methods that
   post needs, plus a `reader_session_id` keyword on `get_message`.

Writer decisions:

7. **Membership is computed in SQL from `sessions`.** It is not stored, because
   live populations change on every session start.
   - `global`: sessions on the channel's machine whose `source` is not
     `system`.
   - `project`: the same, restricted to the channel's project.
   - `tree`: every session whose topmost `parent_session_id` ancestor is the
     channel's root. Clear successors are included.
   - `direct`: the two endpoints. A self channel has a single endpoint.
   This reads the sessions family's table from another family's schema
   functions. #21562 (Sessions and transcripts) owns those columns, and the
   functions name exactly `id`, `machine_id`, `project_id`, `source`, `status`,
   `parent_session_id`, and `created_at`. Channels are machine-scoped, as
   `src/gobby/sessions/mailbox_targets.py::resolve_broadcast_selection` is
   today.
8. **Default subscription levels** are `mentions` for `global` and `project`,
   and `all` for `tree` and `direct`. A `mentions` member sees posts that
   mention it or are marked `broadcast`. A `mute` member sees nothing pending.
   History stays readable at every level.
9. **Wake defaults by channel kind.** Each post stores `wake` as `default`,
   `all`, or `none`.
   - `default`: `direct` and `tree` posts wake every member that can see them;
     `global` and `project` posts wake only mentioned members.
   - `all`: wakes every member that can see the post.
   - `none`: wakes nobody.
   These reproduce `send_message`'s current wake contract:
   - a direct send with `wake` omitted wakes its recipient;
   - an omitted fanout wake stays queued, because broadcasts mention nobody;
   - `wake=true` maps to `all`, and `wake=false` maps to `none`.
   Only sessions in a live status (`LIVE_SESSION_STATUS_ORDER`) are woken.
10. **Ordering and acknowledgement.**
    - A BEFORE INSERT trigger locks the channel row, takes `next_seq`, and sets
      `sent_at = clock_timestamp()`. Within a channel, `seq` order is therefore
      commit order.
    - `seq` can have gaps: a deduplicated `INSERT … ON CONFLICT (id) DO NOTHING`
      still consumes a number. Every reader pages by `seq > after_seq` and
      never assumes contiguity.
    - Acknowledgement is contiguous per channel. The new cursor is the greater
      of the old cursor and the largest acknowledged `seq` below the smallest
      still-unacknowledged visible pending `seq`.
    - A budget-deferred message, or a concurrent out-of-order acknowledgement,
      therefore never skips an unread message. Delivery is at least once, and
      duplicates happen only under a race.
11. **Messages move; nothing is dropped at cutover.** Migration 461 copies
    every `inter_session_messages` row, ids included, into its direct channel
    in `(sent_at, id)` order. It sets each recipient's cursor to the highest
    copied `seq` below that recipient's first undelivered row, then drops the
    table. A session that is live across the restart keeps its pending mail.
12. **Retention treats non-live sessions as settled.** Pruning deletes a
    message older than the cutoff once no session in a live status has it
    pending. This replaces the zombie expiry loop, which existed only to mark
    rows to closed sessions as delivered. That loop is deleted in 1.2.
13. **The `messaging` family defaults to native.** `RouteTable::new` defaults
    every family to `Proxy`, and Python has no `/api/messaging`. `RouteFamily`
    gains `default_backend()`, which returns `Proxy` unless the family
    overrides it, and `messaging` overrides it to `Native`. A bootstrap
    `front_door.routes.messaging` entry still wins.
14. **`gmsg` exports constants and a service; gdaemon implements
    `RouteFamily`.** The trait lives in gdaemon, so a family crate that
    implemented it would depend on gdaemon and form a cycle. This follows
    `crates/gterminals/src/lib.rs` (`FAMILY_NAME`, `ROUTE_PREFIXES`). 1.5
    corrects `crates/AGENTS.md`, which still says a family exports a static
    `RouteFamily`.
15. **Identity on `/api/messaging`.** The caller is the principal the front
    door verified under #23273 (Hub-side key validation, front-door identity,
    and shared-token cutover). The acting session is `X-Gobby-Session-Id`.
    - Only an operator principal may act for a session. That covers the Python
      shims, which call with `gobby.utils.local_token.daemon_auth_headers`, and
      operator clients.
    - Agent capability tokens get 403 `agent_principal_unsupported` until D2
      (#21570) serves the tools natively.
16. **Consensus protocol.** A council thread lives in the `project` channel,
    with the seats mentioned. It uses three typed message kinds:
    - `proposal`: metadata `plan_path`, `plan_sha256` (the SHA-256 of the
      committed file bytes), `commit`, and `version`.
    - `objection`: metadata `proposal_id`, `severity` (`blocking` or `nit`),
      and `finding_id`.
    - `accept`: metadata `proposal_id` and `plan_sha256`.

    Consensus holds when the latest proposal has an accept from a session other
    than its author that names its hash, and no blocking objection naming that
    proposal follows the accept. Each proposal version is one round.
    `get_consensus` reports the state, the round, and the objections. There is
    no round cap: the seats still send the coordinator any disagreement they
    cannot resolve.
17. **The attestation round survives.** Consensus attests the narrative hash.
    The Writer's dated V1 entry cites the accept message id, the hash, and the
    commit. Before deriving M1, the Adversary confirms that the V1 commit
    differs from the accepted commit only inside `## V1 Plan Changelog`. M1
    derivation and application are unchanged (`review.md`, `approval.md`).
18. **The human's role.** The Program Director and Josh can read the thread and
    post in it. The Program Director rules on escalated disagreements, and
    Josh approves after stamping. There are no per-finding votes.
19. **No live board here.** A board view that updates live needs the native WS
    transport. It is D10, owned by #21558 (Native WS transport in gdaemon).
20. **Reply-mode coordination waits retire after their consumers move.**
    `wait_for_message(target="session", from_session=<owner>)` replaces
    `wait_for_coordination(reply=true)`. Migration 461 first retargets the
    reply branch to `agent_messages`, so it keeps working. 1.7 switches every
    consumer and then removes the mode (migration 462).

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and never runs the full suite. Rust database tests read
  `GOBBY_SCHEMA_TEST_DATABASE_URL` and skip without it, following
  `crates/gcore/src/postgres_pool/tests.rs`. No leaf restarts the running
  daemon, touches port 60891, or reads `~/.gobby/local_cli_token` or
  `~/.gobby/bootstrap.yaml`.
- **Rust.** Load the `rust` skill before editing `crates/`, where
  `crates/CLAUDE.md` governs. `cargo build`, `clippy`, and `nextest` are heavy
  work under `.gobby/roles/_common.md`.
- **Schema identity.** Each of migrations 460, 461, and 462 updates the full
  carrier set in its own leaf, following commit `b5a436f0e3`. The carriers
  are:
  - the migration file, `catalog.manifest.json`, and
    `crates/gcore/src/schema/assets.rs::MIGRATIONS`;
  - `crates/gcore/src/grant/bundle.rs` (`GOLDEN_LATEST_CHECKSUM` and the
    `latest_version` in `expected_schema_identity`);
  - the two identity contract tests, `src/gobby/storage/schema_expected_identity.json`,
    and `tests/contracts/http/runtime_handshake.json`;
  - the five `tests/runtime_grants/golden/*.json` vectors.
  `baseline.sql` is not regenerated. Whichever epic lands a migration second
  renumbers and regenerates.
- **Adapter ceiling.** `src/gobby/storage/inter_session_messages.py` stays at
  or under 471 lines, and its public method signatures stay unchanged except
  for the additions in Decision 6.
- **Large files are not targets.** `src/gobby/events/wake.py` (937 lines),
  `src/gobby/hooks/inbox.py` (983 lines), and
  `src/gobby/sessions/compact_continuation.py` (887 lines) read messages only
  through the adapter, so no leaf edits them.
- **No secrets in output.** The shims never log a bearer or a header value.
- **No backward compatibility.** 0.5.0 has not shipped. The old table, the
  zombie loop, and reply-mode waits leave without aliases. Message rows move
  (Decision 11).
- **Consumer sweeps** ran read-only on `0.5.0` at `d3985d1130` (2026-10-05):
  - `gcode grep -F` for `inter_session_messages`, `cleanup_zombie_messages_loop`,
    `_zombie_messages_task`, `resolve_broadcast_selection`,
    `wait_for_coordination`, and `reply=true`;
  - `gcode grep -w` for `get_undelivered_wake_recipients`,
    `get_undelivered_wake_messages`, `CoordinationWaitService`, and
    `add_messaging_tools`.
  - The importers of `InterSessionMessageManager` keep their calls. They are
    listed under 1.2's Consumers unchanged.

## Requirement Mapping
`kind: framing`

| #22340 validation item | Here |
| --- | --- |
| Channel/member/message schema with per-member cursors replacing fan-out | 1.1, 1.2, 1.3; Decisions 7, 10, 11 |
| `wait_for_message` driven by `pg_notify` | 1.1 (trigger), 1.5 (registration), 1.6 (tool and seam); Decision 3 |
| Subscription levels and mention routing within the 6500-character piggyback budget | 1.1 (visibility, contiguous ack), 1.6 (labels); Decisions 8, 10 |
| Wake defaults per channel kind | 1.1, 1.5, 1.6; Decision 9 |
| Planner/adversary consensus protocol and its relationship to attestation | 1.4 (fold), 1.7 (templates); Decisions 16 to 18 |
| RouteFamily crate placement and Stage 2 dependencies | 1.4, 1.5; Decisions 4, 13, 14, 15 |
| Python glue inventory that dies at S2.11/S2.12 and after | Python Glue Inventory; D1 to D11 |

## Python Glue Inventory
`kind: framing`

| Glue | Paths | Deleted by | Section |
| --- | --- | --- | --- |
| Hook piggyback | `hooks/event_enrichment.py`, `hooks/pending_messages.py`, `hooks/pending_message_reservations.py`, `hooks/receipt_effects.py`, `hooks/grok_pending_context.py`, `hooks/hook_manager.py`, `hooks/inbox.py` | #21569 (S2.11) | D1 |
| MCP tool registrations | `mcp_proxy/tools/agent_messaging.py`, `mcp_proxy/tools/agent_channels.py`, `mcp_proxy/tools/coordination.py`, `mcp_proxy/registries.py` | #21570 (S2.12) | D2 |
| Wake seam, mailbox, agent-run messages | `events/coordination_waits.py`, `storage/coordination_waits.py`, `storage/agent_message_waits.py`, `events/wake.py`, `events/wake_notifications.py`, `events/wake_recovery.py`, `sessions/mailbox.py`, `sessions/mailbox_targets.py`, `sessions/mailbox_delivery.py`, `agents/resume_finalization.py`, `hooks/session_coordinator.py`, `runner_init/orchestration.py` | #21564 (S2.7) | D3 |
| Session continuity | `sessions/compact_continuation.py`, `servers/websocket/handlers/session_observe_proxy.py` | #21562 | D4 |
| Rules engine | `workflows/engine/templating.py`, `workflows/engine/run_command_effects.py`, `workflows/enforcement/blocking.py` | #21567 | D5 |
| Tasks | `mcp_proxy/tools/tasks/_stage_review.py`, `servers/routes/tasks_assignment.py` | #21560 | D6 |
| Communications | `storage/communications.py`, `storage/decision_answers.py` | #21583 | D7 |
| Web chat | `servers/websocket/server.py`, `servers/websocket/chat/_pending_messages.py` | #21592 | D8 |
| Retention loop | `runner_maintenance/messaging.py` | #21582 | D9 |
| The adapter and its backend wiring | `storage/inter_session_messages.py`, `storage/__init__.py`, `runner_init/servers.py`, `servers/http.py` | #21574 | D11 |

All paths are under `src/gobby/`. D10 (live board) adds a capability and
deletes no glue.

## P1: Channels replace the per-recipient mailbox
`kind: framing`

**Goal**: every agent message lives once in a channel, readers advance their
own cursors, and the council's consensus is a checkable state.

### 1.1 Channel schema, visibility, cursors, wake rules, and waits in SQL [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/460_agent_channels.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog entries for migration 460
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/src/grant/bundle.rs::GOLDEN_LATEST_CHECKSUM`
- `crates/gcore/src/grant/bundle.rs::expected_schema_identity`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: packaged expected identity at version 460
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: handshake records the schema identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/storage/test_agent_channels_schema.py`

**Research context:**
- The current table is `inter_session_messages`:
  - columns `id`, `from_session`, `to_session` (all `uuid`), `content`,
    `priority`, `sent_at`, `message_type`, `metadata_json jsonb`, and
    `delivered_at`;
  - both session FKs `ON DELETE CASCADE DEFERRABLE`, four indexes, and grants
    to `gobby_daemon_runtime` (`baseline.sql`).
- `sessions` provides `id`, `machine_id`, and `project_id` (all `uuid`;
  `project_id NOT NULL`), plus `source`, `status`, `parent_session_id`, and
  `created_at`.
- `LIVE_SESSION_STATUS_ORDER` (`src/gobby/storage/sessions/_constants.py`) is
  `active`, `paused`, `interrupted`, `awaiting_input`, `awaiting_approval`, and
  `awaiting_handoff`. `SYSTEM_SESSION_SOURCE` is `system`.
- Coordination waits (migrations 431, 441, 447) resolve through
  `resolve_coordination_wait` and the trigger `coordination_message_committed`
  on the old table. 1.1 leaves them alone; 1.2 retargets them.
- `src/gobby/hooks/pending_messages.py::render_pending_messages` renders in the
  given order and defers a suffix once the 6500-character budget is spent.
  Only the represented ids are acknowledged, through
  `receipt_effects.apply_acknowledged_receipt` and then `mark_delivered_batch`.

**Implementation:** migration `460_agent_channels.sql` is additive. All tables
grant `SELECT, INSERT, UPDATE, DELETE` to `gobby_daemon_runtime`, and functions
grant `EXECUTE` to the same role.

- Tables:
  - `agent_channels`:
    - `id uuid` primary key, and `kind` (`global`, `project`, `tree`, or
      `direct`);
    - `machine_id` (for `global` and `project`) and `project_id` (for
      `project`);
    - `root_session_id` (for `tree`), plus `peer_low_session_id` and
      `peer_high_session_id` (for `direct`, with low ≤ high and equal for a
      self channel);
    - `next_seq bigint NOT NULL DEFAULT 1` and `created_at`.
    A CHECK fixes which columns are set for each kind. Partial unique indexes
    allow one channel per machine, per (machine, project), per root, and per
    peer pair. Session FKs cascade.
  - `agent_channel_members`:
    - `(channel_id, session_id)` primary key;
    - `level` (`all`, `mentions`, or `mute`);
    - `delivered_seq bigint NOT NULL DEFAULT 0` and `delivered_at`.
    Rows are created on first acknowledgement or subscription change. A missing
    row means the default level and cursor 0.
  - `agent_messages`:
    - `id uuid` primary key, assignable by the client;
    - `channel_id`, `seq bigint NOT NULL` (unique per channel), and
      `from_session` (FK `ON DELETE CASCADE DEFERRABLE`);
    - `thread_root_id` (FK to `agent_messages` `ON DELETE SET NULL`);
    - `content`, `priority`, `message_type`, and `metadata_json`;
    - `mentions uuid[] NOT NULL DEFAULT '{}'`,
      `broadcast boolean NOT NULL DEFAULT false`, `wake` (`default`, `all`,
      or `none`), and `sent_at`.
    Indexes cover `(from_session, sent_at DESC)` and `(thread_root_id, seq)`.
  - `agent_message_waits`:
    - `id`, `waiter_session_id`, `channel_id`, `after_seq`, and optional
      `from_session` and `thread_root_id`;
    - `expires_at`, `outcome` (`waiting`, `message`, `timeout`, or
      `cancelled`), `message_id` (`ON DELETE SET NULL`);
    - `created_at`, `completed_at`, and `delivered_at`.
- `agent_messages_before_insert` (BEFORE INSERT trigger) locks the channel row,
  assigns `seq` from `next_seq`, increments `next_seq`, and sets
  `sent_at = clock_timestamp()`. It rejects a `thread_root_id` from another
  channel.
- Functions, which the adapter (1.2) and `gmsg` (1.4) both call:
  - `agent_tree_root(session)`: the topmost ancestor through
    `parent_session_id`, with a `CYCLE` guard.
  - `agent_channel_for(kind, actor, peer DEFAULT NULL, project DEFAULT NULL)`:
    get or create. `project` uses `coalesce(project, actor.project_id)`.
  - `agent_channel_member(channel, session)`: the Decision 7 rule.
  - `agent_messages_visible(session, pending_only)`: messages the session can
    read, ordered by `(sent_at, channel_id, seq)`.
    - A row is returned when the session is a member, the message is not its
      own (except in a self channel), and, for `global` and `project`,
      `created_at <= sent_at`.
    - Its effective level must also allow the message (Decision 8).
    - When `pending_only` is set, the row also needs `seq > delivered_seq`.
    - Each row carries `channel_kind`, `reader_session_id`, and `wakes`, the
      Decision 9 rule for this reader.
  - `agent_messages_ack(session, ids uuid[]) RETURNS SETOF uuid`: the
    contiguous rule (Decision 10) under a member-row lock. It upserts the
    member row, sets `delivered_at` when the cursor advances, and returns the
    acknowledged ids now at or below the cursor.
  - `agent_message_recipients(message_id)`: rows of `session_id`, `status`, and
    `wakes` for live-status members that can see the message, ordered by
    `created_at, id`.
  - `agent_wake_pending_sessions()`: live sessions with a pending message whose
    `wakes` is true, ordered by first `sent_at`.
  - `agent_messages_prune(cutoff, max_rows)`: Decision 12.
  - `resolve_agent_message_wait(wait_id)`:
    - completes a `waiting` row with the first visible message for which
      `seq > after_seq`, the `from_session` matches when one is set, and the
      `thread_root_id` matches when one is set;
    - otherwise times the row out once `expires_at` has passed.
- `agent_messages_after_insert` (AFTER INSERT trigger) resolves the matching
  waits on the channel and calls `pg_notify('gobby_agent_message_wait', wait_id)`
  for each one it completes.
- Regenerate the carriers as the Constraints list them.

**Granularity:** one leaf. Migration 460 is one schema unit. Its tables,
triggers, and functions form one lifecycle (post, visibility, acknowledgement,
wake, wait), and it touches one production file plus the derived carriers.
The eight acceptance items each test one invariant of that unit.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_agent_channels_schema.py -q`,
then `cargo nextest run -p gobby-core --test schema_contract` and
`cargo nextest run -p gobby-daemon --test cli_contract` (heavy work).

**Acceptance:**

- 1.1.1 - `agent_channel_for` returns the same channel for repeated calls of each kind, orders direct peers, and refuses a shape the kind does not allow. test: `tests/storage/test_agent_channels_schema.py::test_channel_for_is_get_or_create_per_kind`.
- 1.1.2 - Concurrent posts to one channel get strictly increasing `seq` in commit order, and a deduplicated insert leaves a gap that history paging tolerates. test: `tests/storage/test_agent_channels_schema.py::test_seq_follows_commit_order_and_tolerates_gaps`.
- 1.1.3 - Membership follows Decision 7: a clear successor is in its tree, a session on another machine is not in `global`, and a system session is never a global recipient. test: `tests/storage/test_agent_channels_schema.py::test_membership_follows_sessions`.
- 1.1.4 - Visibility applies levels, mentions, `broadcast`, own posts, and the `created_at` rule. A `mentions` member never sees unmentioned chatter. test: `tests/storage/test_agent_channels_schema.py::test_visibility_applies_levels_and_mentions`.
- 1.1.5 - An acknowledgement that skips a budget-deferred message, and two concurrent out-of-order acknowledgements, both leave every unacknowledged message pending. test: `tests/storage/test_agent_channels_schema.py::test_ack_is_contiguous_under_deferral_and_races`.
- 1.1.6 - `wakes` and `agent_wake_pending_sessions` follow the Decision 9 table for every kind and every `wake` value, and they exclude sessions that are not live. test: `tests/storage/test_agent_channels_schema.py::test_wake_rule_follows_channel_kind`.
- 1.1.7 - A post that satisfies a waiting row completes it and notifies `gobby_agent_message_wait` in the same transaction. A filtered wait ignores other senders and threads. An expired wait resolves to `timeout`. test: `tests/storage/test_agent_channels_schema.py::test_message_wait_resolves_and_notifies`.
- 1.1.8 - Pruning keeps a message any live session still has pending and deletes it once only non-live sessions have it pending. The packaged schema identity reports version 460. test: `tests/storage/test_agent_channels_schema.py::test_prune_keeps_live_pending_rows`.

### 1.2 The adapter reads channels, and `inter_session_messages` is dropped [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/461_retire_inter_session_messages.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog entries for migration 461
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/src/grant/bundle.rs::GOLDEN_LATEST_CHECKSUM`
- `crates/gcore/src/grant/bundle.rs::expected_schema_identity`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: packaged expected identity at version 461
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: handshake records the schema identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `src/gobby/storage/inter_session_messages.py::*` — scope-reason: every query moves to the channel tables behind unchanged signatures
- `src/gobby/agents/resume_finalization.py::notify_parent_of_recovery`
- `src/gobby/hooks/session_coordinator.py::SessionCoordinator.complete_agent_run`
- `src/gobby/storage/decision_answers.py::_ON_THIS_MACHINE`
- `src/gobby/storage/decision_answers.py::_close_mailbox_row`
- `src/gobby/workflows/engine/templating.py::TemplatingMixin._has_pending_messages`
- `src/gobby/workflows/engine/templating.py::TemplatingMixin._pending_message_count`
- `src/gobby/runner_maintenance/messaging.py::cleanup_zombie_messages_loop`
- `src/gobby/runner_maintenance/__init__.py`
- `src/gobby/runner_lifecycle_periodic.py::HUB_ONLY_PERIODIC_TASKS`
- `src/gobby/runner_lifecycle_periodic.py::_default_loops`
- `src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks`
- `src/gobby/runner_lifecycle.py::run_daemon`
- `src/gobby/runner_lifecycle_shutdown.py::_cancel_periodic_tasks`
- `src/gobby/runner_init/storage.py::open_storage_and_config`
- `src/gobby/runner.py::*` — scope-reason: removes only the `_zombie_messages_task` attribute annotation on `GobbyRunner`
- `tests/storage/test_inter_session_messages.py::*` — scope-reason: every adapter test now runs over channel tables
- `tests/test_runner_bin_freshness.py::*` — scope-reason: drops the `cleanup_zombie_messages_loop` stub
- `tests/test_runner_lifecycle_periodic.py::HUB_ONLY`
- `tests/sessions/test_mailbox.py::*` — scope-reason: raw reads of `inter_session_messages` move to `agent_messages`
- `tests/hooks/test_session_coordinator.py::TestAgentRunCompletion.test_complete_agent_run_uses_latest_inter_session_message`
- `tests/servers/routes/test_tasks_routes.py::TestLifecycleMutations.test_claim_task_creates_assignment_mailbox_message_and_wake`
- `tests/workflows/test_messaging_rules.py::_insert_undelivered_message`
- `tests/workflows/test_messaging_rules.py::_insert_delivered_message`
- `tests/storage/test_agent_messages_cutover.py`

**Research context:**
- Adapter methods and their current queries
  (`src/gobby/storage/inter_session_messages.py`, 471 lines):
  - `create_message(from_session, to_session, …, message_id)`, `get_message`,
    `get_messages`, and `has_completion_notification`;
  - `get_undelivered_messages`, `has_unread_without_read_since`,
    `get_undelivered_wake_messages`, and `get_undelivered_wake_recipients`
    (both wake methods read `metadata_json ->> 'wake_requested'`);
  - `mark_delivered_batch(message_ids, to_session) -> list[str]`,
    `delete_delivered_before(cutoff, limit)`, `list_messages(session_id,
    direction, undelivered_only, message_type, limit, offset)`, and
    `mark_delivered`.
- The raw-SQL readers outside the adapter:
  - `notify_parent_of_recovery` uses
    `INSERT … ON CONFLICT (id) DO NOTHING RETURNING id` with a uuid5 dedupe id.
  - `SessionCoordinator.complete_agent_run` falls back to the latest `content`
    sent by the finishing session.
  - `decision_answers._ON_THIS_MACHINE` joins `delivery.id = answer.id` and
    `asker.id = delivery.to_session`. `_close_mailbox_row` updates
    `delivered_at`.
  - `TemplatingMixin._has_pending_messages` and `_pending_message_count` read
    undelivered rows.
  - `cleanup_zombie_messages_loop._expire_zombies` marks rows to old closed or
    expired sessions delivered.
- The zombie loop is wired through the following, and `tests/test_runner_bin_freshness.py`
  stubs it:
  - `runner_maintenance/__init__.py` (re-export);
  - `runner_lifecycle_periodic.py` (`HUB_ONLY_PERIODIC_TASKS` name
    `zombie-message-cleanup`, `_default_loops`, `start_periodic_tasks`);
  - `runner_lifecycle.py::run_daemon` (import and kwarg);
  - `runner_lifecycle_shutdown.py::_cancel_periodic_tasks`;
  - `runner_init/storage.py::open_storage_and_config` and the
    `GobbyRunner._zombie_messages_task` attribute.
- Coordination SQL:
  - 441 redefines `resolve_coordination_wait(wait_id, reply_message_id)` and
    creates `coordination_message_committed` AFTER INSERT on the old table.
  - 447 rewrites the release lookup
    (`SELECT id INTO release_id FROM inter_session_messages AS message …`).
  - Release means a `coordination_release` message from owner to waiter whose
    metadata carries `coordination_key`.

**Implementation:**
- Migration `461_retire_inter_session_messages.sql`:
  1. Copies the old rows into direct channels and sets cursors (Decision 11).
     The `wake` column is `all` where `metadata_json ->> 'wake_requested'` is
     `true`, else `none`.
  2. Redefines `resolve_coordination_wait`, keeping its signature, to read
     `agent_messages` joined to direct channels. A release or reply is a
     message in the waiter's direct channel with the owner whose `seq` is
     greater than the channel head at registration. 461 adds
     `coordination_waits.after_seq` for that, and existing waiting rows take
     the copied head.
  3. Recreates `coordination_message_committed` as AFTER INSERT on
     `agent_messages`.
  4. Drops `inter_session_messages` with its indexes, FKs, and grant.
- The adapter keeps every public signature and its `InterSessionMessage` and
  `InterSessionMessageManager` names. `InterSessionMessage` gains
  `channel_id`, `channel_kind`, `seq`, `thread_root_id`, and `mentions`. For a
  direct row, `to_session` is the peer of `from_session`.
  - `create_message` inserts into `agent_channel_for('direct', from, to)`.
    `wake` follows `metadata_json.wake_requested` as in step 1.
  - `get_messages`, `get_undelivered_messages`, `has_completion_notification`,
    `has_unread_without_read_since`, and `list_messages` read
    `agent_messages_visible`. `has_unread_without_read_since` reads the member
    row's `delivered_at`.
  - `get_undelivered_wake_messages` filters `wakes`.
    `get_undelivered_wake_recipients` reads `agent_wake_pending_sessions()`.
  - `mark_delivered_batch` and `mark_delivered` call `agent_messages_ack`.
    `delete_delivered_before` calls `agent_messages_prune`.
- Raw-SQL readers:
  - `notify_parent_of_recovery` inserts into `agent_messages` with
    `agent_channel_for('direct', child, parent)`, keeping its uuid5 id and
    `ON CONFLICT (id) DO NOTHING`.
  - `complete_agent_run` selects from `agent_messages`.
  - `_ON_THIS_MACHINE` joins `agent_messages` and its direct channel, taking
    the asker as the peer of the sender.
  - `_close_mailbox_row` calls `agent_messages_ack` for that asker.
  - `_has_pending_messages` and `_pending_message_count` read
    `agent_messages_visible(%s, true)`.
- Delete `cleanup_zombie_messages_loop` and its wiring in every target listed
  above (Decision 12). `HUB_ONLY` drops `zombie-message-cleanup`.
- The test targets that read or insert `inter_session_messages` rows with raw
  SQL use `agent_messages` and `agent_channel_for('direct', …)` instead.
- Regenerate the carriers.

Consumers unchanged:
- `src/gobby/events/wake.py` — no-edit-reason: calls adapter methods whose signatures are unchanged.
- `src/gobby/events/wake_notifications.py` — no-edit-reason: calls adapter methods whose signatures are unchanged.
- `src/gobby/events/wake_recovery.py` — no-edit-reason: reads wake recipients and wake messages through unchanged adapter methods.
- `src/gobby/hooks/event_enrichment.py` — no-edit-reason: the piggyback reads pending rows through the adapter in the same order.
- `src/gobby/hooks/receipt_effects.py` — no-edit-reason: acknowledges through `mark_delivered_batch`, now contiguous in SQL.
- `src/gobby/hooks/grok_pending_context.py` — no-edit-reason: reads pending rows through the adapter.
- `src/gobby/hooks/hook_manager.py` — no-edit-reason: constructs the adapter only.
- `src/gobby/sessions/compact_continuation.py` — no-edit-reason: uses adapter methods whose signatures are unchanged.
- `src/gobby/sessions/mailbox_delivery.py` — no-edit-reason: consumes `InterSessionMessage` objects only.
- `src/gobby/storage/communications.py` — no-edit-reason: creates and reads messages through the adapter.
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py` — no-edit-reason: sends through the adapter.
- `src/gobby/servers/routes/tasks_assignment.py` — no-edit-reason: sends through the adapter.
- `src/gobby/servers/websocket/handlers/session_observe_proxy.py` — no-edit-reason: reads through the adapter.
- `src/gobby/servers/websocket/chat/_pending_messages.py` — no-edit-reason: `mark_delivered` keeps its signature.
- `src/gobby/workflows/engine/run_command_effects.py` — no-edit-reason: uses the adapter only.
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: constructs the adapter only.
- `src/gobby/runner_init/servers.py` — no-edit-reason: constructs the adapter only.
- `src/gobby/agents/resume_executor.py` — no-edit-reason: calls `notify_parent_of_recovery`, whose signature is unchanged.
- `src/gobby/runner_lifecycle_reconcile.py` — no-edit-reason: calls `notify_parent_of_recovery`, whose signature is unchanged.
- `tests/agents/test_lifecycle_task_completion.py` — no-edit-reason: calls `complete_agent_run` through its unchanged signature with no raw message rows.
- `tests/hooks/test_session_lookup_metadata.py` — no-edit-reason: calls `complete_agent_run` through its unchanged signature with no raw message rows.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: re-exports `open_storage_and_config` by name.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: calls `open_storage_and_config` and never reads `_zombie_messages_task`.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: calls `open_storage_and_config` and never reads `_zombie_messages_task`.
- `tests/runner_init/test_runner_init_storage.py` — no-edit-reason: calls `open_storage_and_config` and never reads `_zombie_messages_task`.
- `tests/test_runner_maintenance_startup.py` — no-edit-reason: starts periodic tasks without naming the zombie loop.
- `tests/test_runner_skill_maintenance.py` — no-edit-reason: starts periodic tasks without naming the zombie loop.
- `tests/test_runner_workflow_audit_maintenance.py` — no-edit-reason: starts periodic tasks without naming the zombie loop.
- `tests/test_runner_approval_timeout.py` — no-edit-reason: starts periodic tasks without naming the zombie loop.
- `tests/test_runner_resource_monitor.py` — no-edit-reason: starts periodic tasks without naming the zombie loop.
- `tests/test_runner_lifecycle.py` — no-edit-reason: runs `run_daemon` and `_cancel_periodic_tasks` without naming the zombie loop or its task attribute.
- `tests/test_runner_pid_file.py` — no-edit-reason: runs `run_daemon` without naming the zombie loop.
- `tests/test_asgi_chat_shutdown.py` — no-edit-reason: cancels periodic tasks without naming the zombie task attribute.
- `tests/test_runner_model_metadata_refresh.py` — no-edit-reason: cancels periodic tasks without naming the zombie task attribute.
- `tests/test_runner_shutdown.py` — no-edit-reason: cancels periodic tasks without naming the zombie task attribute.

**Granularity:** one leaf. Dropping the table and retargeting every reader of
it is one atomic cutover: any reader left behind fails on the first query after
the migration. The production files are the adapter, four raw-SQL readers, and
the zombie loop's wiring, which must go with the table it updates. Seven
acceptance items each cover one reader family or invariant.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_inter_session_messages.py tests/storage/test_agent_messages_cutover.py tests/events/test_coordination_waits.py tests/sessions/test_mailbox.py tests/test_runner_bin_freshness.py tests/test_runner_lifecycle_periodic.py tests/hooks/test_session_coordinator.py tests/servers/routes/test_tasks_routes.py tests/workflows/test_messaging_rules.py -q`,
then `gcode grep -F "inter_session_messages" src crates/gcore/assets/schema/migrations`.
The only hits are migrations at or below 461, plus the adapter module's own
name and its imports.

**Acceptance:**

- 1.2.1 - Migration 461 moves every row with its id into the matching direct channel. Undelivered rows stay pending for their recipient, delivered rows do not, and the old table is gone. test: `tests/storage/test_agent_messages_cutover.py::test_rows_move_with_cursors_and_table_drops`.
- 1.2.2 - Every adapter method keeps its signature and return type over channel tables. A direct row's `to_session` is the peer. The module stays at or under 471 lines. test: `tests/storage/test_inter_session_messages.py::test_adapter_contract_over_channels`.
- 1.2.3 - `mark_delivered_batch` returns only the ids at or below the new cursor, and leaves a budget-deferred id pending. test: `tests/storage/test_inter_session_messages.py::test_mark_delivered_batch_is_contiguous`.
- 1.2.4 - `notify_parent_of_recovery` deduplicates by its uuid5 id, and `complete_agent_run`'s fallback returns the child's latest message. test: `tests/storage/test_agent_messages_cutover.py::test_recovery_and_completion_readers_use_channels`.
- 1.2.5 - Decision answers on this machine still list and close, and the rules engine's pending helpers count visible pending messages. test: `tests/storage/test_agent_messages_cutover.py::test_decision_answers_and_rule_helpers_use_channels`.
- 1.2.6 - Keyed release, status, and reply coordination waits resolve from `agent_messages`, and a waiting row registered before the migration still resolves. test: `tests/events/test_coordination_waits.py::test_coordination_waits_resolve_from_agent_messages`.
- 1.2.7 - The zombie loop and its periodic task are gone, and retention prunes per Decision 12. test: `tests/test_runner_bin_freshness.py::test_periodic_tasks_have_no_zombie_message_loop`.

### 1.3 A `global` or `project` send writes one post [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/sessions/mailbox.py::MailboxService.send`
- `src/gobby/sessions/mailbox_targets.py::resolve_broadcast_selection`
- `src/gobby/storage/inter_session_messages.py::*` — scope-reason: adds `create_channel_message` and the `reader_session_id` keyword on `get_message`
- `src/gobby/mcp_proxy/tools/agent_messaging.py::add_messaging_tools`
- `tests/sessions/test_mailbox.py::*` — scope-reason: fanout tests assert one post
- `tests/mcp_proxy/tools/test_agent_messaging_broadcast.py::*` — scope-reason: broadcast results come from one post
- `tests/mcp_proxy/tools/test_agent_messaging.py::*` — scope-reason: adds the channel-reader test for `get_inter_session_message`

**Research context:**
- `MailboxService.send` (`src/gobby/sessions/mailbox.py`, 809 lines) resolves
  the target inside one transaction, then calls `create_message` once per
  recipient with a shared `broadcast_id` in metadata. With `wake`, it calls
  `dispatch_mailbox_wakes`.
- `dispatch_mailbox_wakes` (`sessions/mailbox_delivery.py`) raises unless
  `len(messages) == len(session_ids)`.
- `resolve_broadcast_selection` validates the sender and project rules:
  - a global send rejects `project_id`;
  - a system project send needs `project_id`;
  - a session project send derives the project from the sender.
- It selects live sessions on the sender's machine that are not the system
  session and not the sender, then returns `selector_metadata` holding
  `scope` (`kind`, `machine_id`, `project_id`) and `recipient_states`.
- The `build` target fans out to build participants. It stays a
  per-recipient direct send.
- `get_inter_session_message` (inside `add_messaging_tools`) lets only the
  sender or the recipient read a message.

**Implementation:**
- `resolve_broadcast_selection` keeps its signature and validation. It now
  resolves the channel with `agent_channel_for('global' | 'project', …)`. Its
  `recipient_states` are the live members of that channel, minus the sender,
  read with the same membership function as 1.1. It returns the channel id
  beside the existing fields.
- The new adapter method `create_channel_message(channel_id, from_session,
  content, priority, message_type, metadata_json, broadcast, wake)` inserts
  one row and returns it. For a `global` or `project` target, `send` calls it
  once with `broadcast=True` and `wake` set to `all` or `none` from its `wake`
  argument, keeping the `broadcast_id` metadata.
- The returned `messages` list repeats that one message once per recipient,
  so `dispatch_mailbox_wakes` keeps its invariant. `MailboxSendResult`'s
  fields are unchanged.
- `get_message(message_id, reader_session_id=None)` sets `to_session` to the
  reader when the reader can see the message. `get_inter_session_message`
  passes the calling session and admits any reader that can see the message.

Consumers unchanged:
- `src/gobby/communications/telegram_actions.py` — no-edit-reason: sends `target="session"`, which stays a direct send.
- `src/gobby/servers/routes/tasks_assignment.py` — no-edit-reason: sends `target="session"`, which stays a direct send.
- `tests/communications/test_communications_manager.py` — no-edit-reason: exercises direct sends only.
- `tests/communications/test_telegram_actions.py` — no-edit-reason: exercises direct sends only.
- `tests/communications/test_telegram_decisions.py` — no-edit-reason: exercises direct sends only.
- `tests/events/test_wake_recovery.py` — no-edit-reason: sends `target="session"` only.
- `tests/events/test_wake_unread_dedup.py` — no-edit-reason: sends `target="session"` only.
- `tests/hooks/test_inline_mcp_dispatcher.py` — no-edit-reason: registers `add_messaging_tools` through its unchanged signature.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_mailbox.py tests/mcp_proxy/tools/test_agent_messaging_broadcast.py tests/mcp_proxy/tools/test_agent_messaging.py -q`.

**Acceptance:**

- 1.3.1 - A `project` send to N live sessions writes one `agent_messages` row, reports the same N recipients and `recipient_states`, and each recipient sees it pending. test: `tests/sessions/test_mailbox.py::test_project_send_writes_one_post`.
- 1.3.2 - A session started after a `global` send does not see it. One started before and still live does. test: `tests/sessions/test_mailbox.py::test_global_post_visibility_follows_created_at`.
- 1.3.3 - `wake=true` wakes every recipient through `dispatch_mailbox_wakes`, and an omitted fanout wake wakes nobody. test: `tests/mcp_proxy/tools/test_agent_messaging_broadcast.py::test_broadcast_wake_contract_is_unchanged`.
- 1.3.4 - `get_inter_session_message` returns a broadcast to any recipient that can see it and refuses a non-member. test: `tests/mcp_proxy/tools/test_agent_messaging.py::test_get_message_admits_channel_readers`.

### 1.4 `gobby-messaging` crate: repository and consensus fold [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gmsg/Cargo.toml`
- `crates/gmsg/src/lib.rs`
- `crates/gmsg/src/model.rs`
- `crates/gmsg/src/repository.rs`
- `crates/gmsg/src/consensus.rs`
- `crates/gmsg/tests/repository.rs`

**Research context:**
- Family crate rules are in `crates/AGENTS.md`, under "Daemon family crates":
  directory `crates/g<family>`, package `gobby-<family>`, `publish = false`,
  and the workspace version. The crate joins the root `members` list when its
  epic starts.
- `crates/gterminals/src/lib.rs` exports `FAMILY_NAME` and `ROUTE_PREFIXES`,
  because `RouteFamily` is defined in gdaemon (Decision 14).
- gcore feature `postgres-pool`:
  `gobby_core::postgres_pool::{Pool, Transaction}`,
  `Pool::transaction(lock, f)`, `Transaction::query`, `query_opt`,
  `query_one`, and `execute`.
- The schema functions come from 1.1.

**Implementation:**
- Root `Cargo.toml` adds `crates/gmsg` to `members`. `crates/gmsg/Cargo.toml`
  declares `gobby-messaging` with `publish = false` and workspace-inherited
  fields. Its dependencies are `gobby-core` (path, feature `postgres-pool`),
  `serde`, `serde_json`, and `thiserror`, all already in the workspace. SQL
  returns ids and timestamps as text, so no uuid or chrono feature is needed.
- `lib.rs` exports `FAMILY_NAME = "messaging"` and
  `ROUTE_PREFIXES = &["/api/messaging"]`, and re-exports the model,
  repository, and consensus items.
- `model.rs` defines:
  - `ChannelKind`, `Level`, `WakeMode`, `Channel`, `Message`, and `Wait`;
  - the request types `PostRequest` (a channel selector or a channel id,
    `content`, `thread_root_id`, `mentions`, `message_type`, `metadata`,
    `priority`, `broadcast`, `wake`), `HistoryQuery` (`after_seq`, `limit`
    defaulting to 50 with a maximum of 200), `SubscriptionRequest`, and
    `WaitRequest` (`channel`, `from_session`, `thread_root_id`, `after_seq`,
    `timeout` defaulting to 900 s with a maximum of 3600 s);
  - `MessagingError` (`NotAMember`, `UnknownChannel`, `Invalid`, and
    `Database`).
- `repository.rs` defines `MessagingRepository<'p>` over `&Pool`. Each method
  is one transaction that calls the 1.1 functions:
  - `channels(actor)` returns the actor's channels with level and pending
    count.
  - `history(actor, channel, query)` returns rows with `seq > after_seq`, for
    members only, at every level.
  - `post(actor, request)` checks membership (or the system session), inserts,
    and returns the message plus `agent_message_recipients` with `wakes`.
  - `set_level(actor, channel, level)` upserts the member row.
  - `register_wait(actor, request)`:
    - inserts with `after_seq` defaulting to the actor's cursor on that
      channel, so an unread match satisfies the wait at once;
    - calls `resolve_agent_message_wait`;
    - when the wait is already satisfied, marks it delivered in the same
      transaction and returns the message, so it never wakes.
  - `thread(actor, root)` returns the thread's messages in `seq` order.
- `consensus.rs` defines `fold(messages) -> ConsensusState`, a pure function
  implementing Decision 16. It returns `state` (`open`, `objected`, or
  `consensus`), `round`, the latest proposal, the accept, the blocking and nit
  objections, and `invalid_message_ids` for typed messages with malformed
  metadata, which the fold ignores.

**Focused verification (planned):**
`GOBBY_SCHEMA_TEST_DATABASE_URL=… cargo nextest run -p gobby-messaging` and
`cargo clippy -p gobby-messaging --all-targets -- -D warnings` (heavy work).

**Acceptance:**

- 1.4.1 - `post` returns the assigned `seq` and the recipients with `wakes`, and refuses a non-member sender with `NotAMember`. test: `crates/gmsg/tests/repository.rs::post_returns_seq_and_recipients`.
- 1.4.2 - `history` pages by `after_seq` across a gap and returns history to a `mute` member. test: `crates/gmsg/tests/repository.rs::history_pages_across_gaps`.
- 1.4.3 - `register_wait` returns an already-unread match immediately and leaves it undelivered to the wake seam, and it otherwise returns `waiting`. test: `crates/gmsg/tests/repository.rs::register_wait_satisfies_from_cursor`.
- 1.4.4 - The fold reports `consensus` only for an accept from a non-author that names the latest proposal's hash and is not followed by a blocking objection. A new proposal reopens the thread and increments the round. test: `crates/gmsg/src/consensus.rs::fold_follows_the_consensus_rule`.
- 1.4.5 - Malformed typed messages are listed in `invalid_message_ids` and do not change the state. test: `crates/gmsg/src/consensus.rs::fold_ignores_malformed_messages`.

### 1.5 gdaemon serves `/api/messaging` natively [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/front_door/messaging.rs`
- `crates/gdaemon/src/front_door/routes.rs::RouteFamily`
- `crates/gdaemon/src/front_door/routes.rs::FAMILIES`
- `crates/gdaemon/src/front_door/routes.rs::RouteTable::new`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: declares the `messaging` module only
- `crates/AGENTS.md`

**Research context:**
- `RouteFamily` (`crates/gdaemon/src/front_door/routes.rs`) has `name`,
  `prefixes`, and `router() -> Router<FrontDoorState>`. `FAMILIES` holds only
  `HealthFamily`.
- `RouteTable::new` takes each family's backend from the bootstrap
  `front_door.routes` map and falls back to `RouteBackend::Proxy`. Unclaimed
  methods under a claimed prefix are proxied.
- `FrontDoorState` holds the backend target and state. #21559 adds the gcore
  pool to it (Decision 4). This leaf reads that field and creates no pool.
- #23273's front-door verification resolves the caller's key or capability
  token (Decision 15).

**Implementation:**
- `RouteFamily` gains `fn default_backend(&self) -> RouteBackend`, defaulting to
  `RouteBackend::Proxy`. `RouteTable::new` falls back to it instead of the
  constant. `FAMILIES` adds `&MessagingFamily`.
- `front_door/messaging.rs` defines `MessagingFamily`. Its name and prefixes
  come from `gobby_messaging::{FAMILY_NAME, ROUTE_PREFIXES}`, and
  `default_backend` returns `Native`. Routes:
  - `GET /api/messaging/channels`;
  - `GET /api/messaging/channels/{channel_id}/messages`;
  - `POST /api/messaging/messages`;
  - `PUT /api/messaging/channels/{channel_id}/subscription`;
  - `POST /api/messaging/waits`;
  - `GET /api/messaging/threads/{root_id}/consensus`, which runs the fold over
    `thread`.
- The actor extractor admits only an operator principal and reads
  `X-Gobby-Session-Id`. A missing session header is 400 `session_required`,
  and an agent principal is 403 `agent_principal_unsupported`.
- Errors map as follows: `NotAMember` to 403, `UnknownChannel` to 404,
  `Invalid` to 400, and an unavailable pool to 503
  `{"status":"unavailable"}`, the front door's typed unavailable body.
- `crates/AGENTS.md` describes the constants pattern and the native default
  for a family with no Python twin (Decisions 13 and 14).

Consumers unchanged:
- `crates/gdaemon/src/serve.rs` — no-edit-reason: calls `RouteTable::new(FAMILIES, routes, &state)`, whose signature is unchanged.

**Focused verification (planned):** `cargo nextest run -p gobby-daemon front_door`
and `cargo clippy -p gobby-daemon --all-targets -- -D warnings` (heavy work).

**Acceptance:**

- 1.5.1 - With no bootstrap entry, `/api/messaging` is served natively and `/api/health` keeps its configured backend. An explicit `front_door.routes.messaging: proxy` still proxies. test: `crates/gdaemon/src/front_door/routes.rs::messaging_family_defaults_native`.
- 1.5.2 - An operator request with a session header posts and reads history. A missing header is 400 `session_required`, and an agent principal is 403 `agent_principal_unsupported`. test: `crates/gdaemon/src/front_door/messaging.rs::actor_rules_are_enforced`.
- 1.5.3 - Repository errors map to 403, 404, 400, and 503 with the bodies above. test: `crates/gdaemon/src/front_door/messaging.rs::errors_map_to_typed_statuses`.
- 1.5.4 - The consensus route returns the fold's state for a thread. test: `crates/gdaemon/src/front_door/messaging.rs::consensus_route_returns_fold_state`.

### 1.6 Agent tools, the wait seam, and channel labels in the piggyback [category: code] (depends: 1.3, 1.5)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/agent_channels.py`
- `src/gobby/mcp_proxy/registries.py::setup_internal_registries`
- `src/gobby/storage/agent_message_waits.py`
- `src/gobby/events/coordination_waits.py::CoordinationWaitService`
- `src/gobby/storage/coordination_waits.py::CoordinationWaitManager`
- `src/gobby/runner_lifecycle.py::run_daemon`
- `src/gobby/hooks/pending_messages.py::_render_message_line`
- `tests/mcp_proxy/tools/test_agent_channels.py`
- `tests/storage/test_agent_message_waits.py`
- `tests/events/test_coordination_waits.py::*` — scope-reason: the service now serves managers keyed by notify channel
- `tests/hooks/test_pending_messages.py::*` — scope-reason: channel labels in rendered lines
- `tests/events/test_coordination_holds.py::*` — scope-reason: the service is constructed with keyed managers

**Research context:**
- `CoordinationWaitService(manager, registry, machine_id, connection_factory)`:
  - runs `LISTEN gobby_coordination_wait` before a recovery snapshot, on each
    reconnect;
  - `process` refreshes the row, schedules a timer while it is waiting, and
    otherwise calls `registry.wake_sessions` with `coordination_wait_payload`
    before marking it delivered;
  - is constructed in `runner_lifecycle.py::run_daemon` for
    `PostgresHubDatabase`, and stopped in `runner_lifecycle_shutdown.py` and
    at the end of `run_daemon`.
- `CoordinationWaitManager` exposes `refresh`, `pending_ids`, and
  `mark_delivered`.
- `wait_for_coordination` (`mcp_proxy/tools/coordination.py`) calls
  `headless_wait_refusal` first and returns the wait payload. The house
  pattern is to register durably and then yield the turn.
- The tool registrations for `gobby-agents` are made in
  `registries.py::setup_internal_registries` (`add_messaging_tools`).
- Python reaches the front door through `gobby.utils.daemon_url.daemon_url`
  and `gobby.utils.local_token.daemon_auth_headers`.
- `pending_messages._render_message_line` renders the priority, the sender,
  and the inline content or a `get_inter_session_message` reference.

**Implementation:**
- `agent_channels.py` defines `add_channel_tools(registry, ctx)`. It is
  registered beside `add_messaging_tools`. Each tool forwards one request to
  `/api/messaging` with the caller's session header and returns the JSON body.
  The tools hold no messaging logic:
  - `read_channel(channel, target_id=None, after_seq=0, limit=50)`;
  - `post_message(channel, content, target_id=None, thread_root_id=None, mentions=None, message_type="message", metadata=None, priority="normal", broadcast=False, wake=None)`.
    `wake` is `None` (`default`), `True` (`all`), or `False` (`none`).
    `post_message` then passes the returned `wakes` recipients to
    `dispatch_mailbox_wakes`.
  - `set_subscription(channel, level, target_id=None)`;
  - `wait_for_message(channel, target_id=None, from_session=None, thread_root_id=None, timeout=900)`.
    It calls `headless_wait_refusal` first. With `channel="session"` and
    `from_session` set, it replaces a reply wait.
  - `get_consensus(thread_root_id)`.
  Channel selectors are `global`, `project`, `tree`, `session` (direct, with
  `target_id`), and `parent`. Mentions accept the same session refs as
  `send_message`.
- `storage/agent_message_waits.py` defines `AgentMessageWaitManager`, with
  `notify_channel = "gobby_agent_message_wait"`. It has `refresh` (via
  `resolve_agent_message_wait`), `pending_ids(machine_id)`, `mark_delivered`,
  and `payload(row)` (the wait id, the outcome, and the message brief).
- `CoordinationWaitManager` gains `notify_channel = "gobby_coordination_wait"`
  and `payload` (`coordination_wait_payload`).
- `CoordinationWaitService` takes managers keyed by `notify_channel`. It
  LISTENs on each key over one connection, queues `(channel, wait_id)`,
  recovers from every manager, and wakes with that manager's payload.
- `run_daemon` passes both managers. The stop calls are unchanged.
- `_render_message_line` prefixes non-direct posts with `[#global]`,
  `[#project]`, or `[#tree]`. It adds ` (thread <first 8 of root>)` for thread
  replies and ` (mentions you)` when the reader is in `mentions`.
- The test targets that construct `CoordinationWaitService` pass the keyed
  managers.

Consumers unchanged:
- `src/gobby/ai/embedding_switch_runner.py` — no-edit-reason: calls `setup_internal_registries`, whose signature is unchanged.
- `src/gobby/servers/http.py` — no-edit-reason: calls `setup_internal_registries`, whose signature is unchanged.
- `tests/config/test_config_runtime_config_resolution.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/dispatch/test_bundled_agent_contract.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/mcp_proxy/test_merge_integration.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/mcp_proxy/test_registries.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/mcp_proxy/test_registries_startup.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/mcp_proxy/test_workspaces_registry.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/mcp_proxy/tools/test_review_learning.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/skills/reference_library_helpers.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: calls `setup_internal_registries` and names no `gobby-agents` tool.
- `src/gobby/agents/idle_check_handler.py` — no-edit-reason: `CoordinationWaitManager` only gains `notify_channel` and `payload`.
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: `CoordinationWaitManager` only gains `notify_channel` and `payload`.
- `src/gobby/hooks/session_completion.py` — no-edit-reason: `CoordinationWaitManager` only gains `notify_channel` and `payload`.
- `src/gobby/workflows/engine/core.py` — no-edit-reason: `CoordinationWaitManager` only gains `notify_channel` and `payload`.
- `tests/storage/test_agent_run_live_stats.py` — no-edit-reason: `CoordinationWaitManager` only gains `notify_channel` and `payload`.
- `tests/test_runner_lifecycle.py` — no-edit-reason: runs `run_daemon` without constructing the wait service.
- `tests/test_runner_pid_file.py` — no-edit-reason: runs `run_daemon` without constructing the wait service.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_agent_channels.py tests/storage/test_agent_message_waits.py tests/events/test_coordination_waits.py tests/events/test_coordination_holds.py tests/hooks/test_pending_messages.py -q`.

**Acceptance:**

- 1.6.1 - Each tool forwards exactly one `/api/messaging` request with the operator credential and the caller's session header, and returns the body unchanged. test: `tests/mcp_proxy/tools/test_agent_channels.py::test_tools_forward_to_messaging_routes`.
- 1.6.2 - `post_message` with `wake` omitted on `project` wakes only mentioned live members, and on `tree` wakes every visible live member. test: `tests/mcp_proxy/tools/test_agent_channels.py::test_post_message_wake_defaults`.
- 1.6.3 - `wait_for_message` refuses headless sessions like `wait_for_coordination`, and returns an already-unread match without a later wake. test: `tests/mcp_proxy/tools/test_agent_channels.py::test_wait_for_message_refuses_headless_and_returns_unread`.
- 1.6.4 - A post that satisfies a waiting row wakes the waiter once through `gobby_agent_message_wait`, and a reconnect recovers waits completed while the listener was down. test: `tests/events/test_coordination_waits.py::test_message_waits_wake_through_notify_channel`.
- 1.6.5 - Coordination waits keep their behavior under the keyed service. test: `tests/events/test_coordination_waits.py::test_coordination_waits_unchanged_under_keyed_service`.
- 1.6.6 - The piggyback labels channel posts and threads. A `mentions`-level project member's pending set excludes unmentioned chatter, so that chatter spends none of the 6500-character budget. test: `tests/hooks/test_pending_messages.py::test_channel_posts_are_labeled_within_budget`.

### 1.7 The plan council converges on a consensus thread, and reply waits retire [category: code] (depends: 1.6)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/462_retire_coordination_reply_waits.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog entries for migration 462
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/src/grant/bundle.rs::GOLDEN_LATEST_CHECKSUM`
- `crates/gcore/src/grant/bundle.rs::expected_schema_identity`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: packaged expected identity at version 462
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: handshake records the schema identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `src/gobby/storage/coordination_waits.py::CoordinationWaitManager.register`
- `src/gobby/mcp_proxy/tools/coordination.py::_resolve_owner`
- `src/gobby/mcp_proxy/tools/coordination.py::register_coordination_tools`
- `src/gobby/install/shared/workflows/agents/plan-writer.yaml::*` — scope-reason: the consensus flow text
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: the consensus flow text
- `src/gobby/install/shared/workflows/rules/stop-gates/require-task-close.yaml::*` — scope-reason: the reply-wait instruction
- `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml::*` — scope-reason: the reply-wait instruction
- `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml::*` — scope-reason: both tool lists
- `src/gobby/install/shared/skills/gobby/references/plan/review.md`
- `src/gobby/install/shared/skills/gobby/references/agents/messaging.md`
- `docs/guides/agents.md`
- `tests/events/test_coordination_waits.py::*` — scope-reason: reply-mode tests become rejection tests
- `tests/events/test_coordination_holds.py::*` — scope-reason: drops the `reply` and `reply_expiry` endings
- `tests/agents/test_idle_coordination_holds.py::test_coordination_hold_resets_exhausted_idle_recovery_then_resumes`
- `tests/e2e/test_external_terminal_attach.py::test_codex_reply_wait_reaches_terminal_after_isolated_restart`
- `tests/install/test_council_consensus_templates.py`

**Research context:**
- Reply-wait consumers:
  - `references/agents/messaging.md:33`, `docs/guides/agents.md:325`, and the
    `wait_for_coordination` description;
  - `require-task-close.yaml:16` and `require-epic-tree-close.yaml:20` ("reply
    from another session").
- `block-tools-after-handoff-compact.yaml` lists `send_message`,
  `end_agent_run`, and `wait_for_coordination` at lines 26 and 116.
- The council flow text is in `plan-writer.yaml` (Flow steps 3 and 4),
  `plan-adversary.yaml` (steps 1 to 4), and `review.md:9`.
  - The Writer drafts, the enhancer edits are disposed, the Writer passes the
    plan to the Adversary, and the two converse over `send_message` until
    consensus.
  - The Writer commits a dated V1 entry and sends the SHA. The Adversary
    derives and applies M1 through `gobby-plans:derive_plan_handoff_manifest`
    and `apply_plan_handoff_manifest`.
- `CoordinationWaitManager.register` enforces exactly one of
  `coordination_key`, `statuses`, or `reply`. `_resolve_owner` defaults the
  owner to the spawner for `reply=true`.

**Implementation:**
- Migration `462_retire_coordination_reply_waits.sql`:
  - cancels any `waiting` reply rows (`outcome = 'cancelled'`);
  - drops `coordination_waits.reply` and its CHECK branch;
  - redefines `resolve_coordination_wait` without the reply branch.
- `register` and the tool drop `reply`. The tool description points to
  `wait_for_message`.
- The council templates:
  1. The Writer posts a `proposal` in the `project` channel, mentioning the
     Adversary and the coordinator. That starts the thread.
  2. The Adversary answers in the thread with `objection` messages carrying a
     severity and a finding id. The Writer posts each revision as a new
     `proposal` in the same thread. Both seats wait with `wait_for_message` on
     the thread.
  3. Consensus is `get_consensus(thread_root_id).state == "consensus"`.
  4. The V1 entry cites the accept message id, the hash, and the commit.
  5. Before deriving M1, the Adversary runs
     `git diff <accepted commit> <V1 commit>` and confirms that only
     `## V1 Plan Changelog` changed.
  6. Unresolved disagreements still go to the coordinator.
- `review.md:9` describes the same flow. `messaging.md` and `agents.md` replace
  reply waits with `wait_for_message` and document channels, levels, mentions,
  wake defaults, and threads. Both stop-gate rules name `wait_for_message` for
  a reply. Both tool lists in `block-tools-after-handoff-compact.yaml` add
  `wait_for_message`.
- `tests/install/test_council_consensus_templates.py` asserts the following:
  - the templates name `post_message`, `wait_for_message`, and
    `get_consensus`, and contain no `reply=true`;
  - every bundled template and reference lacks `reply=true` for
    `wait_for_coordination`.
- The holds tests drop their `reply` and `reply_expiry` endings. The idle
  holds test drops its `reply` ending. The e2e restart test waits with
  `wait_for_message(channel="session", target_id=<owner>, from_session=<owner>)`
  and keeps its restart and terminal-delivery assertions.

Consumers unchanged:
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: registers keyed or status waits only.
- `tests/storage/test_agent_run_live_stats.py` — no-edit-reason: registers keyed or status waits only.
- `src/gobby/mcp_proxy/tools/agents_registry.py` — no-edit-reason: calls `register_coordination_tools` through its unchanged signature.
- `tests/agents/test_headless_coordination_waits.py` — no-edit-reason: exercises the headless refusal without `reply`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/events/test_coordination_waits.py tests/events/test_coordination_holds.py tests/agents/test_idle_coordination_holds.py tests/install/test_council_consensus_templates.py -q`,
then `uv run pytest tests/e2e/test_external_terminal_attach.py -k reply_wait -q`,
then `gcode grep -F "reply=true" src docs/guides` returns no coordination
hits.

**Acceptance:**

- 1.7.1 - Migration 462 cancels waiting reply rows and removes the column. `register` and the tool reject `reply`. test: `tests/events/test_coordination_waits.py::test_reply_mode_is_retired`.
- 1.7.2 - Keyed and status coordination waits still resolve after 462. test: `tests/events/test_coordination_waits.py::test_keyed_and_status_waits_survive_reply_retirement`.
- 1.7.3 - The Writer and Adversary templates and `review.md` specify the proposal, objection, and accept protocol, the `get_consensus` check, the V1 citation, and the diff check before M1. test: `tests/install/test_council_consensus_templates.py::test_council_templates_specify_consensus_thread`.
- 1.7.4 - No bundled template, reference, rule, or guide tells an agent to use `wait_for_coordination(reply=true)`. test: `tests/install/test_council_consensus_templates.py::test_no_reply_wait_instructions_remain`.

## D1 Hook piggyback reads channels natively
`kind: deferred`

- D1.1: hook ingress renders pending channel messages from
  `agent_messages_visible` within the 6500- and 2000-character budgets. It
  acknowledges them with `agent_messages_ack` after the provider accepts the
  hook output.
- D1.2: the mailbox parts of `src/gobby/hooks/event_enrichment.py`,
  `pending_messages.py`, `pending_message_reservations.py`,
  `receipt_effects.py`, `grok_pending_context.py`, `hook_manager.py`, and
  `inbox.py` are deleted.

```yaml
deferral:
  task_ref: "#21569"
  reason: "Hook ingress moves to gdaemon in S2.11; until then the piggyback reads channels through the adapter (Orchestrator ruling, 12:52 CT)."
  owner: "gobby-1.0 stage 2 (S2.11, #21569)"
  original_acceptance_items:
    - D1.1
    - D1.2
```

## D2 Messaging tools served natively
`kind: deferred`

- D2.1: `send_message`, `get_inter_session_message`,
  `get_inter_session_messages`, `read_channel`, `post_message`,
  `set_subscription`, `wait_for_message`, `get_consensus`,
  `wait_for_coordination`, and `cancel_coordination_wait` are served natively.
  `src/gobby/mcp_proxy/tools/agent_messaging.py`, `agent_channels.py`,
  `coordination.py`, and their registration in `registries.py` are deleted.
- D2.2: `/api/messaging` admits agent capability principals, each acting only
  as its own session.

```yaml
deferral:
  task_ref: "#21570"
  reason: "The MCP front door flip (S2.12) is where tools stop being Python registrations; the Orchestrator ruled the shims' deletion moves there (12:56 CT)."
  owner: "gobby-1.0 stage 2 (S2.12, #21570)"
  original_acceptance_items:
    - D2.1
    - D2.2
```

## D3 Wake delivery, the mailbox, and agent-run messages move to gdaemon
`kind: deferred`

- D3.1: gdaemon LISTENs on `gobby_agent_message_wait` and
  `gobby_coordination_wait` and delivers wakes natively.
  `src/gobby/events/coordination_waits.py`,
  `src/gobby/storage/coordination_waits.py`, and
  `src/gobby/storage/agent_message_waits.py` are deleted.
- D3.2: live wake dispatch and replay are native. The mailbox paths of
  `events/wake.py`, `events/wake_notifications.py`, `events/wake_recovery.py`,
  and `sessions/mailbox_delivery.py` are deleted.
- D3.3: target resolution, the clear-successor redirect, and agent-run
  messages are native. That covers `sessions/mailbox.py`,
  `sessions/mailbox_targets.py`,
  `agents/resume_finalization.py::notify_parent_of_recovery`, the
  `SessionCoordinator.complete_agent_run` fallback, and the
  `runner_init/orchestration.py` wiring.

```yaml
deferral:
  task_ref: "#21564"
  reason: "Wake delivery and agent attention are S2.7's family; the Orchestrator ruled the wake seam's retirement moves there (12:56 CT)."
  owner: "gobby-1.0 stage 2 (S2.7, #21564)"
  original_acceptance_items:
    - D3.1
    - D3.2
    - D3.3
```

## D4 Session continuity reads channels natively
`kind: deferred`

- D4.1: compact continuation and the session observe proxy read pending and
  historical messages from `gmsg`. The adapter calls in
  `src/gobby/sessions/compact_continuation.py` and
  `src/gobby/servers/websocket/handlers/session_observe_proxy.py` are deleted.

```yaml
deferral:
  task_ref: "#21562"
  reason: "These callers belong to the sessions family (#21562, Sessions and transcripts), which owns the session columns the channel functions read."
  owner: "gobby-1.0 stage 2 (#21562)"
  original_acceptance_items:
    - D4.1
```

## D5 Rules engine message queries move with the rules family
`kind: deferred`

- D5.1: the pending-message template helpers in
  `src/gobby/workflows/engine/templating.py`, the adapter use in
  `run_command_effects.py`, and `MESSAGE_DELIVERY_TOOLS` in
  `workflows/enforcement/blocking.py` are native.

```yaml
deferral:
  task_ref: "#21567"
  reason: "Rules evaluation moves in #21567 (Workflows, rules, pipelines, build, and validation)."
  owner: "gobby-1.0 stage 2 (#21567)"
  original_acceptance_items:
    - D5.1
```

## D6 Task review and assignment messages move with the tasks family
`kind: deferred`

- D6.1: `src/gobby/mcp_proxy/tools/tasks/_stage_review.py` and
  `src/gobby/servers/routes/tasks_assignment.py` post through `gmsg`, and their
  adapter use is deleted.

```yaml
deferral:
  task_ref: "#21560"
  reason: "Both callers belong to the tasks family (#21560, Tasks family)."
  owner: "gobby-1.0 stage 2 (#21560)"
  original_acceptance_items:
    - D6.1
```

## D7 Communications and decision answers move with the comms family
`kind: deferred`

- D7.1: `src/gobby/storage/communications.py` and
  `src/gobby/storage/decision_answers.py` read and post through `gmsg`, and
  their adapter use and channel SQL are deleted.

```yaml
deferral:
  task_ref: "#21583"
  reason: "Telegram and Slack delivery move in #21583 (Communications route family: Telegram and Slack)."
  owner: "gobby-1.0 stage 2 (#21583)"
  original_acceptance_items:
    - D7.1
```

## D8 Web chat pending messages move with the WS chat family
`kind: deferred`

- D8.1: `src/gobby/servers/websocket/chat/_pending_messages.py` and the
  adapter wiring in `src/gobby/servers/websocket/server.py` are deleted. Web
  chat acknowledges through `gmsg`.

```yaml
deferral:
  task_ref: "#21592"
  reason: "Web chat moves in #21592 (WS chat route family)."
  owner: "gobby-1.0 stage 2 (#21592)"
  original_acceptance_items:
    - D8.1
```

## D9 Message retention runs in the scheduler
`kind: deferred`

- D9.1: the gdaemon scheduler calls `agent_messages_prune`, and
  `cleanup_comms_messages_loop`'s adapter call in
  `src/gobby/runner_maintenance/messaging.py` is deleted.

```yaml
deferral:
  task_ref: "#21582"
  reason: "Periodic maintenance moves in #21582 (Cron and scheduler route family)."
  owner: "gobby-1.0 stage 2 (#21582)"
  original_acceptance_items:
    - D9.1
```

## D10 Live channel board
`kind: deferred`

- D10.1: gdaemon pushes channel posts over the native WS transport, so a board
  view of `global`, `project`, and `tree` channels and their threads updates
  live.

```yaml
deferral:
  task_ref: "#21558"
  reason: "A live board needs the native WS transport (#21558, Native WS transport in gdaemon); history and posting already work over /api/messaging (Decision 19)."
  owner: "gobby-1.0 stage 2 (S2.2, #21558)"
  original_acceptance_items:
    - D10.1
```

## D11 Delete the adapter
`kind: deferred`

- D11.1: once D1 to D9 have removed every caller,
  `src/gobby/storage/inter_session_messages.py`, its export in
  `src/gobby/storage/__init__.py`, and its construction in
  `src/gobby/runner_init/servers.py` and `src/gobby/servers/http.py` are
  deleted.

```yaml
deferral:
  task_ref: "#21574"
  reason: "The adapter is the last Python messaging code and goes with the backend (#21574, Retire gobby-backend)."
  owner: "gobby-1.0 stage 3 (S3.4, #21574)"
  original_acceptance_items:
    - D11.1
```

## Deferral Finalization
`kind: framing`

After Josh approves and expansion applies, the Orchestrator runs these steps.
The plan's root is #22340. Drafting creates no tasks, labels, or edges.

1. Re-parent #22340 under #21544, and add the epic-level blocked-by edge from
   #22340 onto #21559 (Decision 4).
2. Add `out-of-scope-for:#22340` to #21544. #21544 is open and outside
   #22340's dependency closure.
3. For each of D1 to D11, on its owner (#21569, #21570, #21564, #21562,
   #21567, #21560, #21583, #21592, #21582, #21558, and #21574):
   - add `deferred-from:gobby-messaging:D<n>`;
   - add `cited-parent:#21544`;
   - set null validation criteria to that section's items as written here, or
     append them to existing criteria.
   A blocked-by edge from #22340 onto these owners would make the epic wait
   for most of Stage 2, and onto #21574 it would form a cycle: #21574 waits on
   #21544, which contains #22340. The cited-parent route validates without
   edges (`src/gobby/plans/deferral.py::_has_valid_cited_parent`).
4. Writer proposal for disposition, which is not part of the 12:56 ruling.
   Leaf dispatch does not inherit an ancestor's blocked-by edges
   (`.gobby/plans/completed/daemon-native-runtime-boundary.md`, Constraints).
   So add blocked-by edges from the created 1.5 leaf onto #21559 and, if it is
   still open, onto #23273. Leaves 1.1 to 1.4 need neither, and 1.6 and 1.7
   follow 1.5 through the manifest.
5. Run `gobby-tasks:check_dependency_cycles`. Then confirm that every D1 to
   D11 deferral validates against #22340 before releasing any leaf.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15469 under the
  Orchestrator rulings of 12:52 and 12:56 CT. Targets and consumers were swept
  read-only on `0.5.0` at `d3985d1130`. The draft is narrative only, with no
  M1.

## V2: Verification
`kind: verification`

After every leaf has passed:

1. Run the focused suites of 1.1 to 1.7 together against the test hub, plus
   `GOBBY_SCHEMA_TEST_DATABASE_URL=… cargo nextest run -p gobby-messaging -p gobby-daemon`
   and `cargo clippy --workspace --all-targets -- -D warnings` (heavy work).
2. Run `uv run gobby plans validate .gobby/plans/gobby-messaging.md -p <root>`.
3. Run `gcode grep -F "inter_session_messages" src` and
   `gcode grep -F "reply=true" src docs/guides`. Hits remain only in the
   adapter module's name and its imports.
4. Make a fresh council pass for a small plan. It must reach
   `get_consensus` state `consensus`, and the Adversary's diff check must pass
   before M1.
