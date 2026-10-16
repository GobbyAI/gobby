Plan artifact: `.gobby/plans/native-ws-transport.md`

# Native WS transport in gdaemon

**Plan ID:** native-ws-transport
**Root task:** #21558 — Native WS transport in gdaemon.
**Status:** canonical narrative with Adv4's M1 for A1 to A3; D1 materialized as P2 (B1 to B3) by the #23881 spec on 2026-10-16, re-anchored at 0.5.0 `7ba471a18c`; M1 entries for B1 to B3 await Adversary derivation, then Orchestrator review and Josh approval.

## C1 Scope and Decision Record
`kind: framing`

The existing front door binds the public HTTP and WS ports but byte-splices WS upgrades to Python. This plan gives Rust ownership of the external upgrade, connection lifecycle, subscriptions, and broadcast fanout. It preserves the public application frames and delegates existing application operations to a private Python bridge.

Decisions confirmed by the Orchestrator through LM7 on 2026-10-06, 18:42 CT, and acknowledged by LM7:

1. Rust owns external transport and subscription state. An explicit internal publication channel separates broadcast batches from direct application replies.
2. Python retains existing application handlers. Terminal operations and relay remain downstream in #23219; this plan does not port chat, voice, terminal, workspace, or MCP operations.
3. #23519 — Shared-token cutover: gdaemon validates keys and Python trusts only the front door — owns bearer/API-key validation and front-door identity stamping. The native WS path uses that front door once. Python never revalidates those keys.
4. The private Python bridge alone validates the browser `gobby_session` cookie using the retained AuthService cookie path. No third client credential verifier is added. Existing credential precedence from #23519 applies when a request contains multiple credentials.
5. The same per-boot front-door secret authenticates daemon-to-daemon control traffic. This is internal process-pair trust, not an alternative bearer or cookie admission path. A valid user API key or cookie alone cannot open the private bridge or publish events.
6. Public frame fields and terminal golden bytes remain unchanged. Subscription acknowledgement `events` arrays represent sets: corpus comparison sorts only that array, matching Python's existing unordered set output. No other array, payload field, or wire bytes are normalized.
7. Codec and broker work can proceed independently. Auth-integrated implementation is a typed deferred tail blocked by #23519 and #23523 — Live activation of the key cutover — plus A1/A2/A3. Both landed source and the separate live-auth activation must be proved before that tail starts. There is no fallback implementation of the retiring local-token verifier.
8. No live restart, binary promotion, push, spawn, or main-checkout write is authorized by this planning assignment. Implementation activation later belongs to the Orchestrator's announced window.

Entry mode describes the public transport, not the credential or the user's UI: `direct` is the dedicated public WS listener; `browser` is the HTTP listener's ASGI `/ws` entry, including bearer clients using that path. Direct begins with an absent subscription attribute; browser begins with initialized-empty subscriptions. Reconnect preserves the entry mode and resets its state, never a previous subscription set.

Downstream: #21562 (Sessions and transcripts), #21568 (Remaining route families) and #23219 (terminal WS operations, relay, and family cutover) are blocked by #21558 itself. The epic closes only after D1's deferred tail lands, so D1's native activation, with gdaemon serving `:60888` and the HTTP `/ws` entry, and the closure of #21558 remove the WS prerequisite from all three. Their other blockers still apply: #21562 and #21568 also wait on #21560 (Tasks family), and #23219 also waits on #23214 (Rust tmux runtime parity), #23216 (terminal WS codec and golden replay), #23217 (native ownership cutover) and #23218 (input-grant finalization) and keeps its `needs-planning` hold. A1 to A3 alone unblock none of them.

Native gterm operation without a daemon is an invariant. Nothing in this plan changes gterm, gclient, JSON-lines control, bincode frames, or their independent connection paths. Future hub-to-node relays may consume the frozen envelope, but no relay, replay log, durable event bus, or node protocol is introduced here.

**Granularity:** three independently testable outcomes precede one gated integration outcome: corpus characterization, pure subscription codec, and bounded fanout. The cutover wires private application connections and publication together because switching only one loses or duplicates events. It does not absorb credential validation or application state machines.

## C2 Research and evidence rules
`kind: framing`

Source inspection uses the sole Lane 3 worktree. Draft branch `task-21558` is based on `lane-3-runbooks` at `766ba94a11b3a5c088668c88e01bb0b6dbc13c51`. Earlier observations at documentation-only HEAD `64f5e46e4cc3ed0138c837f9d77d1d595e8b3cac` have identical referenced source bytes. A read-only diff against `0.5.0` found no WS/server source differences; only `crates/gdaemon/tests/cli_contract.rs` differed in the inspected directories. Recheck the integration anchors after #23519 lands; the new auth surface is planned by that task, not present in this draft's source baseline.

Refresh (2026-10-08, W4 gobby#15528): every E1 anchor was re-read on the main checkout at 0.5.0 `3987d09dcc4311a1ebda7d58a47d885a9413560a`. The codec and broker anchors (E3 to E7, E9, E10, E12, E13, E15, E19 to E21) are unchanged apart from line numbers. #23519's front-door cutover has landed (`23328c7392` and later merges): `FrontDoor::handle` authenticates before splice (E2), the direct WS listener checks the forwarded identity (E14), and AuthService checks the verified front-door identity before Authorization and the cookie (E22). #23519 is still open and #23523 is still blocked, so D1's gate is unchanged. The other anchors moved lines only.

Every existing implementation anchor below cites a source evidence entry in E1. Names explicitly marked new are proposed, not claims about existing symbols. The task's older terminal corpus path is stale: the observed corpus is `tests/fixtures/terminal_ws_golden/`, used by the Rust `corpus` helper in E9.

Literal consumer sweeps already run:

- `gcode grep -F 'BroadcastMixin' src/gobby tests -m 30`: server mixin/import; tests in agents/attention metadata, MCP messaging, config-values routes, websocket broadcast, and terminal composer-proof events.
- `gcode grep -F '_handle_subscribe' src/gobby tests -m 30`: dispatch in the WS server, subscription tests, and terminal composer-proof tests.
- `gcode grep -w 'verify_bearer' src/gobby tests -m 30`: AuthService internal checks, API-key/configuration-effective routes and auth-service tests. Those retiring checks belong to #23519, not this plan.

The first three outcomes add modules/harnesses and leave production Python behavior unchanged. The cutover retains the signatures and producer methods of BroadcastMixin; publishers need no event-specific rewrites. Its fresh sweep must distinguish direct replies from calls through `broadcast(message, frames=...)`, rather than classifying messages by their public `type`.

## P1: Characterize and implement native transport primitives
`kind: framing`

### A1 Shared subscribe/unsubscribe/broadcast corpus [category: test]
`kind: deliverable`

Targets:
- `tests/fixtures/ws_envelope_golden/manifest.json`
- `tests/fixtures/ws_envelope_golden/cases.json`
- `tests/servers/test_ws_envelope_golden.py`

**Research context:** E3/E4 show additive subscription sets and clear-all unsubscribe; E5 shows wildcard, gated event types, parameter selectors, and hook-event selectors. E6 filters on the original message but emits its ordered raw fragment frames. The current terminal corpus location and shared-manifest reader are E9. Existing Python subscription methods remain the characterization oracle; no product edits are part of this outcome.

Implement a versioned data corpus containing named state transitions for both public entry modes. Direct-listener connections begin with the subscriptions attribute absent; browser ASGI connections begin with an initialized empty set (E20). An unsubscribe before the first subscribe returns an empty acknowledgement on the absent state but leaves the attribute absent. An explicit internal `None` state also filters everything; a subscribe initializes it, while unsubscribe raises through the existing generic handler-error path and leaves it `None`. Include these absent/explicit-null/initialized distinctions, subscribe with omitted/empty events, duplicate and additive subscriptions, exact type, wildcard, parameter match/mismatch with string equality, hook subtype, non-event messages before/after initialization, remove-one and clear-all unsubscribe. Include ordinary and fragmented broadcasts whose filter message differs from each fragment. Store expected state, recipient sets and exact emitted UTF-8 frame strings. Include direct replies with an event-like type to establish that direct delivery bypasses broadcast filtering.

The unchanged-production parity domain is JSON object controls with a string `type` and an omitted `events` or an array containing only strings. Also characterize the existing non-list-container error. Member-domain bugs are not parity requirements: unhashable/non-string members may mutate or fail in Python today and were routed by Adv2 to the Orchestrator. A1 does not prescribe or alter that unsupported behavior; A2 adds separate native negative cases with a deliberate all-or-nothing validation rule. Record this boundary in the corpus manifest instead of silently treating malformed-member divergence as exact parity.

The Python harness executes each transition against the real HandlerMixin and BroadcastMixin behavior with isolated fake clients. The explicit-null unsubscribe case records the handler exception and exercises the real connection wrapper's generic error path (E12), rather than inventing an acknowledgement. It compares acknowledgement objects after sorting only `events`, asserts exact remaining fields, and compares broadcast frame strings byte-for-byte. Every manifest case is executed exactly once, and an unknown schema version or unlisted/missing case fails. Do not re-record expected output automatically or add corpus cases that prescribe retired token behavior.

**Acceptance:**

- A1.1 - Every declared corpus case replays through the existing Python implementation with the documented single acknowledgement-set normalization. test: `tests/servers/test_ws_envelope_golden.py::test_python_replays_envelope_case`.
- A1.2 - Both entry modes and absent/explicit-null/initialized transitions, including unsubscribe-before-subscribe, replay without modifying production behavior. file: `tests/fixtures/ws_envelope_golden/cases.json`.
- A1.3 - Broadcast and fragment strings compare byte-identically, and direct responses are distinguished from broadcasts. test: `tests/servers/test_ws_envelope_golden.py::test_frame_bytes_and_direct_delivery`.
- A1.4 - Version and case inventory drift fails rather than silently skipping data. test: `tests/servers/test_ws_envelope_golden.py::test_manifest_is_complete`.

**Verification (planned):** protected focused pytest on the new harness plus `tests/servers/test_websocket_subscriptions.py`; no daemon or real credentials. This is standalone characterization infrastructure, so `tdd: false`.

### A2 Native subscription codec and corpus replay (depends: A1) [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/websocket/mod.rs`
- `crates/gdaemon/src/websocket/subscriptions.rs`
- `crates/gdaemon/tests/ws_envelope_golden.rs`

**Research context:** E3/E4 and E5 are the exact Python semantics. In particular, `None` subscriptions differ from an initialized empty set: the latter admits non-event broadcasts. The public event set includes workspace, terminal, session usage/token, agent messages, cron and trace. E6 requires filtering once on the original message and retaining fragment order. The existing Rust corpus reader pattern is E9. The crate library has no indexed symbol-bearing records; its bare Target registers the new module. All websocket module symbols in this outcome are new.

Add new `SubscriptionState` with absent, explicit-null and initialized-set variants, `apply_subscription_request`, and `matches_broadcast`. The constructor takes the trusted public entry mode: direct => absent; browser => initialized empty. Explicit-null is exercised by the internal parity harness and is never selected by a public client. Subscribe initializes absent/null even for an empty array. Unsubscribe on absent acknowledges empty without initialization; unsubscribe on explicit-null emits the generic handler error with unchanged state. Reproduce additive/clear-all transitions, selector splitting at the first colon/equal sign, and string-only parameter equality.

Parse only transport controls here; application frames remain opaque. Validate the complete member domain before mutation: `events` must be omitted (meaning `[]`) or an array of strings. Null, boolean, numeric, array and object members, including mixed valid-invalid lists, return `{"type":"error","code":"ERROR","message":"events must be a list of strings"}` and leave state intact for both subscribe and unsubscribe. This intentionally tightens Python's unsupported malformed-member behavior; supported-domain parity remains exact. Validate a control's `type` as a string before dispatch; null/numeric/array/object type values return the same error shape with message `Message type must be a string`. Invalid JSON and non-object JSON retain the existing `Invalid JSON format` / `Message must be a JSON object` messages. Explicit-null unsubscribe uses message `Internal server error`. Transport-control errors do not invent a request ID; E21 supplies the current error envelope. Validate once, then mutate once; no partial set update, panic, or implicit subscription.

Replay the same A1 corpus in Rust, with the same narrowly defined acknowledgement set normalization and byte-exact broadcast output. Keep event-kind declarations and selector rules in this single module; do not create a generic protocol registry or change the public schema version. This codec has no socket, auth, database, or process dependencies. New unit tests include malformed control inputs and distinguish an unknown application type from a transport control request.

**Acceptance:**

- A2.1 - Rust replays every A1 case with identical transition, recipient and output results. test: `crates/gdaemon/tests/ws_envelope_golden.rs::rust_replays_envelope_corpus`.
- A2.2 - Both public entry modes, absent/explicit-null/initialized transitions and unsubscribe-before-subscribe retain their distinct outcomes. symbol: `SubscriptionState` in `crates/gdaemon/src/websocket/subscriptions.rs`.
- A2.3 - Every non-string member/type class, mixed-member list and invalid JSON/object produces the specified bounded error without partial mutation; native-only negative cases are distinguished from supported-domain parity. test: `crates/gdaemon/tests/ws_envelope_golden.rs::malformed_control_does_not_mutate_state`.
- A2.4 - Application frames are not reserialized or interpreted as subscription controls. test: `crates/gdaemon/tests/ws_envelope_golden.rs::application_frames_are_opaque`.

**Verification (planned):** `cargo test -p gobby-daemon --test ws_envelope_golden`, plus A1's protected Python harness. `tdd: true`, implementation_domain backend. Expected RED is the missing codec; GREEN is shared-corpus parity.

### A3 Bounded native broadcast fanout (depends: A2) [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/websocket/mod.rs`
- `crates/gdaemon/src/websocket/broker.rs`
- `crates/gdaemon/tests/ws_broker.rs`

**Research context:** E6 broadcasts one filter message plus ordered frame strings; E7/E8 bound each stalled send to two seconds and close to one second. E10 gives existing message-size and ping defaults, and E17 gives the current gclient assembly bounds. A2 supplies the pure subscription predicate. This outcome has no public listener or credential checks, so it can land without #23519. All broker symbols and its test harness are new.

Add new `BroadcastBroker` with one owned entry per authenticated logical connection, using A2's subscription state and a per-connection FIFO writer queue. Registration requires an explicit caller-supplied authenticated connection handle; this module cannot discover principals or admit network clients. Serialize subscription updates and publication selection under a short state lock, release it before any socket I/O, and order each selected batch atomically in the writer queue. A subscription success acknowledgement is queued with the state update before later matching publications, so the client never receives newly selected events ahead of its acknowledgement.

Use immutable shared frame bytes per batch. Bound each client's queue to 16 pending batches and 64 MiB of frame bytes; reject an oversize batch before it can consume queue space. Queue overflow or send timeout retires only that client, closes with 1011 within one second, and invokes one cleanup callback. Preserve the existing two-second send bound. The byte ceiling matches the current gclient aggregate assembly bound (E17), allowing its 16 MiB fragmented payloads plus wire encoding rather than imposing a smaller voice/frame ceiling. No config knob or retries are introduced. Publish completion means a batch was enqueued or its client was retired, not delivered. Disconnect, publication and timeout races retire an entry exactly once. No replay after reconnection; each fresh connection resets to its entry-mode initial state, direct absent or browser initialized empty.

**Acceptance:**

- A3.1 - Only matching registered clients receive a batch; fragment bytes and per-client order remain exact. test: `crates/gdaemon/tests/ws_broker.rs::fanout_preserves_selection_and_frame_order`.
- A3.2 - A subscription acknowledgement precedes publications selected by its new state. test: `crates/gdaemon/tests/ws_broker.rs::acknowledgement_precedes_newly_selected_events`.
- A3.3 - A stalled/overflowing client cannot block healthy recipients or grow queues beyond both limits. test: `crates/gdaemon/tests/ws_broker.rs::slow_client_isolated_and_bounded`.
- A3.4 - Simultaneous disconnect and failure invokes cleanup once, and each entry mode reconnects with its own original initial state. test: `crates/gdaemon/tests/ws_broker.rs::retirement_is_once_and_reconnect_is_fresh`.
- A3.5 - Direct replies use the same ordered writer without subscription filtering. test: `crates/gdaemon/tests/ws_broker.rs::direct_reply_bypasses_event_filter`.

**Verification (planned):** `cargo test -p gobby-daemon --test ws_broker --test ws_envelope_golden`; fake time and in-memory writers, no live server. `tdd: true`, implementation_domain backend.

## P2: Auth-integrated private bridge and native WS activation
`kind: framing`

This phase materializes the D1 fixed specification (#23881) as three implementation leaves, re-anchored at 0.5.0 `7ba471a18c` on 2026-10-16. Gates are met: A1 #23878, A2 #23879 and A3 #23880 are closed, #23519 closed 2026-10-09 and #23523's live activation closed 2026-10-11 with a valid receipt.

Two landings since the D1 draft change its runtime anchors. #23457 (`fde8c6182d`) moved daemon lifecycle into gdaemon and deleted `src/gobby/runner_front_door.py`. gdaemon is now the long-lived parent: `serve.rs` mints the per-boot front-door secret (E32), `RuntimeState::prepare` hands it to the Python backend as `GOBBY_FRONT_DOOR_SECRET` (E33), `BackendChild::spawn` delivers a fresh per-spawn supervisor token through `TokenSlot` (E34, E48), `BackendSupervisor::supervise` respawns Python and publishes `BackendStatus` on a watch channel (E35), and Python exits when the supervisor pipe closes (E36). Python incarnations come and go under one gdaemon boot; the reverse no longer happens. #24444 added per-family `front_door.routes` proxy/native/compare backends and the compare log; it registers no WS family.

Decision record. Decisions 1 and 4 to 11 are decided by the Orchestrator's ruling of 2026-10-16 05:44 CDT. Decisions 2, 3 and 12 were confirmed by Security gobby#16011 on rev 2 (receipt `5ada6c18`) and again on rev 4 (receipt `b1d59137`), and accepted by the Orchestrator. Decision 13, added in rev 3, passed Lane 8 review by gobby#16912 and the Security rev 4 confirmation, and is decided by the Orchestrator's confirmation of 2026-10-16 06:25 CDT. Decision 14 is decided under the Orchestrator's NWS-F6 rulings of 2026-10-16 06:26 and 06:27 CDT; its values are new admission limits covered by plan approval.

1. Native WS state lives in gdaemon for its whole boot. Each Python incarnation binds once from `WebSocketServer.start`; there is no child monitor, incarnation counter or notification registration (the old item 8 machinery had no home after #23457).
2. Process-pair proof for every private route is the loopback peer, the per-boot secret in `X-Gobby-Front-Door` (constant time), and, on the gdaemon routes, the current supervisor token in `X-Gobby-Backend-Token` checked with `TokenSlot::authorize`. A valid user key or cookie alone opens nothing private. Lane 1 (LM1 gobby#15990, 2026-10-16) agreed to reusing the header and token and treats the auth entry points below as stable; Security gobby#16011 confirmed this on rev 2 (receipt `5ada6c18`) and rev 4 (receipt `b1d59137`).
3. gdaemon mints a fresh random 128-bit generation on each accepted bind and records the supervisor token that proved it. An accepted bind retires the previous generation. On any change of the runtime `BackendStatus` watch, gdaemon retires the current generation when its proving token no longer passes `TokenSlot::authorize` (E63), so a status change racing a fresh bind never retires that bind. Every generation check and the effect that depends on it commit under one binding lock (B2 item 1). Bind and publish re-run `TokenSlot::authorize` on their presented token inside that lock as part of the commit, so a request proven with a token that rotated while it was in flight commits nothing. A generation is never reused.
4. No `front_door.routes` family is added for application WS. Native transport is unconditional wherever `gdaemon serve` runs (`front_door.enabled`); Python-only mode (`front_door.enabled: false`) keeps today's Python listeners and fanout. The `terminal_ws` latch is untouched, and terminal messages ride the bridge as opaque application frames. Rejected: a `ws` family with proxy/native/compare, because C1 decision 6 forbids a new toggle and byte-comparing stateful streams has no meaning.
5. Python is in native mode exactly when the environment carries both a non-empty `GOBBY_SUPERVISOR_FD` and a non-empty `GOBBY_FRONT_DOOR_SECRET`; only `gdaemon serve` supplies both. Mode is decided from the environment, never from `supervisor_write_token()`, because `watch_supervisor` fills that token asynchronously in a task created before `GobbyRunner.create` runs `init_servers` (E61, E62). The token is read at each private request instead.
6. The private leg is itself a WebSocket from gdaemon to Python `/_gobby/ws/bridge`. Python's first frame is exactly one text `bridge_ready` control. After it, frames map one-to-one (text to text, binary to binary, close to close), with no outer JSON envelope and no base64. Trusted context travels as headers on the private upgrade, built from an allowlist. This supersedes the old item 4 record format: WebSocket framing already separates text from binary, and the first-frame rule stops spoofing. gdaemon validates and consumes exactly the first private frame as readiness; every later private text or binary frame is opaque application data, including one identical to `bridge_ready`, so readiness cannot change admission or generation after frame one. Binary frames stay binary through the broker (B2 item 4).
7. Path claims:
   - HTTP listener: `/ws` and `/ws/{path}` upgrades become native browser entry. `/api/nodes/channel` keeps its existing native handling, and every other upgrade, including `/__vite_hmr` and `/__vite_hmr/{path}` (E45), keeps the existing splice.
   - Direct WS listener: every upgrade path becomes native direct entry, because Python's `websockets` listener ignored the path. Non-upgrade requests on it get a native 426.
   - Reserved `/_gobby/` prefix on both listeners: POST `/_gobby/ws/bind` and `/_gobby/ws/publish` are native. Any other `/_gobby/` path returns 404 and is never forwarded.
   - Node mode (`Door::Node`) keeps its relay unchanged.
8. In native mode Python does not start its `websockets` listener, and both public listeners target the backend HTTP port. `backend_ports` stays unchanged in gcore and Python. Rejected: dropping its WS member, because a gobby-core change forces patch bumps of gcode, ghook and gclient for no behavior change.
9. The native public handshake negotiates no extension and no subprotocol. Python offered permessage-deflate (E49); message payloads stay unchanged.
10. Python publishes to `http://127.0.0.1:{daemon_port}`, or `http://[::1]:{daemon_port}` when `bind_host` is an IPv6 literal. gdaemon always serves a loopback listener for the daemon port and allows plaintext from loopback peers.
11. A failed or ambiguous publish fails that generation in Python and closes its bridge legs concurrently within one second. The publisher then rebinds immediately, then every five seconds until success or shutdown. No application batch is retried or replayed.
12. Bridge admission calls only existing AuthService entry points:
   - Direct entry requires `AuthService.verify_ws_identity` to return a user ID, applying `AuthMixin._authenticate`'s three checks in order with their exact status and body (E52), as a refusal before 101.
   - Browser entry uses `AuthService.is_request_authenticated` on a reconstructed `websocket` scope. Break-glass is therefore ineligible (E40), and a managed bearer on `/ws` is refused by the capability matrix before the cookie is consulted (E39). Refusal is 101, then close 4401 "Authentication required".
   - No verifier is added and none is retired.
13. Server-side subscriptions reach the broker in order. Python handlers add gated parametric subscriptions as a side effect of application requests: `workspace_attach` adds `workspace_event:workspace_id=<id>` and `attach_to_session` adds `session_message:session_id=<id>` and `hook_event:session_id=<external id>` (E54 to E56). gclient never subscribes to `workspace_event` and relies on that side effect (E57 to E59). In native mode each such addition becomes a leg-scoped `subscribe` operation in the publisher's single ordered operation queue, beside the `publish` operations for broadcasts, so it is applied before any later-enqueued publish and acknowledged before the handler sends the reply that confirms the attach. The publisher keeps one request in flight and sends everything queued as one ordered request, so a burst costs one loopback round trip per drain instead of one per broadcast; agent PTY output reaches `broadcast` once per read chunk (E66, E67). Rejected: a Python-to-gdaemon control frame on the bridge leg, because that leg and the publish channel are separate connections with no ordering between them, so a publish could overtake the subscription.
14. **Publisher admission budgets.** The publisher's one ordered queue (decision 13) admits work against three fixed budgets that count queued plus in-flight operations of the current generation, checked synchronously before each enqueue. They are module constants in `native_publish.py`, with no configuration and no second queue:
    - Retained encoded bytes: 128 MiB (134,217,728). This reuses the per-request body ceiling, so the publisher never holds more encoded data than one maximum request.
    - Retained operations: 65,536. This bounds per-operation bookkeeping regardless of payload size. At the two-second request deadline it absorbs 32,768 operations per second, so only a stalled sender reaches it.
    - Waiting futures: 65,536. Each retained operation holds exactly one future, released when its request is answered or its generation fails, so the operation cap bounds futures with the same counter.
   Overflow takes the existing `fail_generation` overload action. Rejected: a configurable limit and a second queue, which add mechanism without a second value to choose.

**Granularity:** three outcomes. B1 adds the Python bridge and publisher modules, inert until wired. B2 adds gdaemon's native transport and private endpoints, unwired. B3 activates both together, because switching admission, dispatch and publication separately loses or duplicates events. B1 and B2 are independently testable against fakes and land without changing live behavior. B3 carries eight acceptance items and seven production files because activation is one atomic switch; its runtime machinery lives in B1's and B2's new files. Existing hand-maintained production files stay below 850 lines; `server.py` is 827 lines today, so B3 adds only delegating calls there and the publisher lifecycle lives in `native_publish.py`.

### B1 Python private bridge and native publisher [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/native_bridge.py`
- `src/gobby/servers/websocket/native_publish.py`
- `src/gobby/servers/_app_ui.py::_mount_ws_endpoint`
- `tests/servers/test_native_ws_bridge.py`
- `tests/servers/test_native_ws_publish.py`
- `src/gobby/servers/websocket/workspace_ws.py::*` — scope-reason: route the workspace_attach subscription additions in _handle_workspace_attach and _read_attach_snapshot through the server-subscription helper
- `src/gobby/servers/websocket/handlers/session_observe_proxy.py::*` — scope-reason: route the attach_to_session subscription additions in handle_attach_to_session through the server-subscription helper
- `docs/contracts/parity-ledger-status.d/websocket-chat-voice-ui.json::*` — scope-reason: add the `/_gobby/ws/bridge` row as proxied
- `docs/contracts/parity-ledger.md`

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: `_mount_ws_endpoint` keeps its signature; the bridge mount happens inside it.
- `tests/servers/test_ws_asgi_endpoint.py` — no-edit-reason: browser `/ws` welcome and auth-close behavior is unchanged in B1; bridge coverage lives in the new bridge tests.
- `tests/servers/test_workspace_ws.py` — no-edit-reason: Python-only attach still adds the same subscription synchronously; the helper returns an already-completed future.
- `tests/servers/test_session_control.py` — no-edit-reason: Python-only attach_to_session subscriptions are unchanged.

**Research context:** gdaemon now supervises Python. It mints the per-boot secret (E32), passes it as `GOBBY_FRONT_DOOR_SECRET` (E33), and delivers a per-spawn supervisor token that Python reads with `supervisor_write_token()` (E34, E36). Native mode is decided from the environment (decision 5): `native_publisher_from_environment()` returns a publisher exactly when `GOBBY_SUPERVISOR_FD` and `GOBBY_FRONT_DOOR_SECRET` are both non-empty, and `None` otherwise. The token itself is filled asynchronously by `watch_supervisor` (E36, E61, E62). Python-only mode is unchanged.

Three handler sites add gated subscriptions to the socket as a side effect (E54 to E56). `_read_attach_snapshot` adds them for every waiter before it yields, so a mutation already queued on the snapshot fence reaches those subscribers, and captures `lifecycle_seq` before returning (E55). gclient subscribes to five event kinds without `workspace_event` and relies on the attached workspace's parametric subscription (E57 to E59).

Browser `/ws` today accepts first and then authenticates with `is_request_authenticated` (close 4401), or closes 1013 when no WS server exists (E50). The direct listener refuses before upgrade: 401 when Authorization is missing, 401 when it is not Bearer, and 403 when no front-door identity is present (E52). `AuthService._legacy_rejection` orders break-glass, then front-door identity, then bearer classification, then cookie (E39). `break_glass_admits` requires an `http` scope (E40). `verified_front_door_identity` compares the secret in constant time and requires all three IDs (E41). `WebSocketServer._handle_connection` registers the client, sends `connection_established`, rebroadcasts pending interactions, and cleans up once (E12). `ASGIWebSocketAdapter` is the adapter shape to mirror: `user_id`, `latency`, `remote_address`, `subscriptions`, `send`, `close` and async iteration (E20). The parity ledger is generated from registered routes; a new route without a shard entry fails `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

Implementation:

1. **Bridge route** (`mount_native_bridge(app, server)`, new). `native_bridge.py` exports it, and `_mount_ws_endpoint` calls it so `/_gobby/ws/bridge` is mounted unconditionally next to `/ws`. It first checks that the actual transport peer (the ASGI `client` of the private connection) is a loopback address, before reading any `X-Gobby-*` header or reconstructing the original client scope. A non-loopback peer, no publisher, a missing or wrong `X-Gobby-Front-Door`, or an `X-Gobby-WS-Generation` other than the publisher's current non-failed generation, the route closes 1008 before running any handler and sends no `bridge_ready`.
2. **Trusted context.** `X-Gobby-WS-Entry` (`direct` or `browser`) selects the entry mode. `X-Gobby-WS-Path` (raw path and query), `X-Forwarded-For` and `X-Forwarded-Proto` rebuild a Starlette `websocket` scope. That scope keeps only Authorization, Cookie, `X-Gobby-Break-Glass`, Origin, User-Agent, `X-Gobby-Front-Door` and the three `X-Gobby-*-Id` identity headers, with `client` taken from `X-Forwarded-For`. `X-Gobby-WS-Leg` carries gdaemon's decimal connection ID for this public connection. Each gdaemon-synthesized header (front-door secret, generation, entry, path, leg, the two forwarded headers and the three identity IDs) must appear at most once, compared case-insensitively; a duplicate closes 1008 before admission.
3. **Admission.**
   - Direct: apply `AuthMixin._authenticate`'s three checks with `AuthService.verify_ws_identity`.
   - Browser: `AuthService.is_request_authenticated(reconstructed)`. The browser `user_id` keeps today's `local-web-<8 hex>` form.
   - Refusals: send `{"type":"bridge_ready","version":1,"generation":G,"admitted":false,...}` with `status` and `body` for direct, or `close_code` 4401 and `reason` "Authentication required" for browser, then close.
   - Admission: send `{"type":"bridge_ready","version":1,"generation":G,"admitted":true}` as the first frame, then run `server.handle_connection(NativeBridgeAdapter)`.
4. **`NativeBridgeAdapter`** (new) mirrors `ASGIWebSocketAdapter`: text and binary pass through unchanged, `subscriptions` starts as an initialized empty set for both entries (Rust filters every broadcast; server-side additions reach Rust through item 6), it records the leg ID, and close and cleanup run once. The adapter registers its leg with the publisher's generation registry and unregisters on exit.
5. **`NativePublisher`** (new) has `start`, `bind`, `publish(message, frames)`, `subscribe(leg, events)`, `fail_generation` and `stop`. `start` and `stop` own the bind and rebind lifecycle so `WebSocketServer` only delegates to them.
   - One FIFO operation queue drained by a single sender task carries every publish and subscribe operation in call order. The sender keeps at most one request in flight and puts every operation queued at send time into that request, in order, up to the 128 MiB body bound. `publish` and `subscribe` enqueue without yielding and return a future that resolves after the 204 for the request carrying that operation, or resolves without sending once its generation has failed; neither future raises.
   - Admission (decision 14). Before enqueueing, `publish` and `subscribe` add the operation's encoded size (its bytes as it will appear in the request body) and one operation to the counters for queued plus in-flight work. If either sum would exceed its budget, the operation is not enqueued, its future resolves without sending, and the publisher calls `fail_generation` for the current generation. A single operation larger than 128 MiB therefore fails its generation at enqueue. The 204 for a request releases its operations' bytes and counts before their futures resolve.
   - `fail_generation` releases all of that generation's accounting at once: queued operations are dropped and their futures resolve without sending, and an in-flight request for it is cancelled and its futures resolve. None of its operations are replayed into the rebind. `stop` does the same for every generation, leaving both counters at zero.
   - Every bind and publish request reads `supervisor_write_token()` when it is sent. `bind` waits at most two seconds for the token to appear (checking every 50 ms); a still-missing token counts as a failed bind.
   - `bind` POSTs `{"max_message_size","ping_interval","ping_timeout"}` (taken from `WebSocketConfig`; 4 KiB body bound) to `/_gobby/ws/bind` with the secret and token headers and a two-second deadline. It returns the generation.
   - Each request POSTs `{"generation","ops":[...]}` to `/_gobby/ws/publish` with the secret and token headers. A publish operation is `{"op":"publish","filter","frames"}`, with frames encoded exactly as `BroadcastMixin.broadcast` encodes them (`json_dumps(frame)` per frame, or `[json_dumps(message)]`, E44). A subscribe operation is `{"op":"subscribe","leg","events"}`. The sender also enforces the 128 MiB encoded-body bound when it assembles each request, uses one existing `httpx.AsyncClient` with a two-second deadline, never retries, and publishes even when no client is connected.
   - Any timeout, transport error or non-204 response atomically marks the generation failed, drops its later queued operations, and closes that generation's registered legs concurrently with 1011 within one second (`asyncio.wait_for` around the gathered closes). It then schedules a rebind: immediately, then every five seconds until success or `stop`. Closing a failed generation never touches legs of a newer one.
   - The publish URL follows decision 10.
6. **Server-side subscriptions** (`add_server_subscriptions(websocket, *events)`, new in `native_bridge.py`). It updates `websocket.subscriptions` exactly as the sites do today (an absent or `None` set becomes a new set first). For a `NativeBridgeAdapter` it also calls `publisher.subscribe(leg, events)` and returns that future; otherwise it returns an already-completed future, so Python-only behavior is unchanged and awaiting it never yields.
   - `handle_attach_to_session` adds both session topics through the helper and awaits it before sending the attach reply.
   - `_read_attach_snapshot` adds the workspace topic for every waiter through the helper before it yields, keeping today's fence rule, and does not await. `_handle_workspace_attach` adds the topic for its own socket through the helper and awaits it before sending the snapshot reply; FIFO order means the waiter-loop additions were applied first.
7. **Ledger.** Add `WS transport /_gobby/ws/bridge` as `proxied`, noting that it is the private bridge into Python application handlers and retires with #21574.

**Acceptance:**

- B1.1 - The bridge refuses a non-loopback transport peer (checked before any header is read or the client scope is rebuilt), a missing or wrong pair secret, an absent, failed or stale generation, and Python-only mode before any application handler runs or any `bridge_ready` frame is sent. test: `tests/servers/test_native_ws_bridge.py::test_bridge_requires_pair_proof_and_current_generation`.
- B1.2 - Admission matches each entry's existing credential owner on the reconstructed context:
  - Direct: missing Authorization gives 401, non-Bearer 401, a managed bearer 403, cookie-only 401, and operator identity is admitted.
  - Browser: operator identity is admitted, a managed bearer with a valid cookie gets 4401, a valid cookie is admitted, an invalid cookie gets 4401, break-glass with a valid cookie is admitted through the cookie only, and break-glass alone gets 4401.
  - Identity headers without the secret are ignored.
  test: `tests/servers/test_native_ws_bridge.py::test_bridge_admission_matrix_matches_entry_owners`.
- B1.3 - Exactly one `bridge_ready` precedes the unchanged `connection_established` welcome. Pending interactions, text and binary bytes pass through exactly. A client text frame typed `bridge_ready` reaches the handler as an application message, and connection cleanup runs once. test: `tests/servers/test_native_ws_bridge.py::test_bridge_preserves_application_connection_lifecycle`.
- B1.4 - The publisher binds with proof and settings within two seconds and turns each broadcast into one publish operation, sent in enqueue order with queued operations coalesced into one request and one request in flight, with frames byte-identical to Python's encoding, including when no client is connected. The 128 MiB bound is checked before sending. test: `tests/servers/test_native_ws_publish.py::test_publisher_binds_and_publishes_encoded_frames`.
- B1.5 - A timed-out, failed or blackholed publish fails only its generation, closes that generation's legs within one second and never retries the batch. It then rebinds immediately and every five seconds, and leaves newer-generation legs untouched. test: `tests/servers/test_native_ws_publish.py::test_ambiguous_publish_retires_generation_without_retry`.
- B1.6 - Server-side subscription additions on a native leg are enqueued before the handler yields and travel ahead of any later-enqueued publish:
  - `attach_to_session` and `workspace_attach` send their reply only after the request carrying that leg's subscribe operation is acknowledged.
  - The `_read_attach_snapshot` waiter additions precede a publish queued on the snapshot fence.
  - Python-only sockets get the same subscription set as today and no request.
  test: `tests/servers/test_native_ws_publish.py::test_server_subscriptions_precede_later_publishes_and_replies`.
- B1.7 - Native mode follows the environment alone. With `GOBBY_SUPERVISOR_FD` and `GOBBY_FRONT_DOOR_SECRET` set and the token arriving after `init_servers`, the publisher exists and `bind` succeeds once the token appears within two seconds; a token that never appears fails the bind into the rebind loop. Without both variables there is no publisher. test: `tests/servers/test_native_ws_publish.py::test_native_mode_from_environment_and_bind_waits_for_token`.
- B1.8 - The publisher queue is bounded by decision 14, shown with a stalled sender and concurrent producers:
  - Retained bytes and operations count queued plus in-flight work. The shipped constants are 128 MiB, 65,536 operations and 65,536 waiting futures; the test lowers them through the module constants to exercise overflow.
  - The first operation that would exceed the byte budget, and separately the operation count, is not enqueued and fails its generation. The queued and in-flight futures of that generation resolve without raising, and its legs close 1011 within one second.
  - The rebind gets a new generation, none of the failed generation's operations are sent in it, and newer-generation legs are untouched.
  - Both counters return to zero after a successful drain, after the failure and after `stop`, and admission still succeeds after more than one budget's worth of cumulative healthy traffic.
  test: `tests/servers/test_native_ws_publish.py::test_publisher_admission_budgets_fail_generation_on_overflow`.

**Verification (planned):** `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_native_ws_bridge.py tests/servers/test_native_ws_publish.py tests/servers/test_ws_asgi_endpoint.py tests/servers/test_workspace_ws.py tests/servers/test_session_control.py tests/test_parity_ledger.py`, with a fake publish server and synthetic secret and token. No daemon and no real credentials are used. Live behavior is unchanged until B3 wires the publisher. `tdd: true`, implementation_domain backend.

### B2 gdaemon native transport and private endpoints (depends: A3) [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/websocket/mod.rs::*` — scope-reason: register the transport and publish modules and update the module doc that today says no listener or auth dependency
- `crates/gdaemon/src/websocket/transport.rs`
- `crates/gdaemon/src/websocket/publish.rs`
- `crates/gdaemon/src/websocket/broker.rs::*` — scope-reason: carry typed text and binary frames in Batch and the writer, and add BroadcastBroker::add_subscriptions for a server-side subscription on one live connection
- `crates/gdaemon/src/websocket/subscriptions.rs::*` — scope-reason: add SubscriptionState::add_server_subscriptions mirroring the Python handler sites
- `crates/gdaemon/tests/ws_broker.rs::*` — scope-reason: add the typed-batch regression with binary interleaved with text, broadcasts and acknowledgements
- `crates/gdaemon/tests/native_ws_transport.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the managed gdaemon version pin by 0.0.1 in step with Cargo.toml

**Research context:** A2 landed `SubscriptionState`, `EntryMode` and `Inbound`. A3 landed `BroadcastBroker` with `register(id, entry, sink, cleanup)`, `receive` (consumes controls and returns application text), `publish(filter, &Batch)` (filter, then enqueue, retiring overflow; E43), `reply`, `disconnect` and `Batch::new` (64 MiB bound). The broker sink type is `tokio_tungstenite::tungstenite::Message`, but a `Batch` holds only `Utf8Bytes` frames and the writer sends each as `Message::Text` (E70), so binary replies need a typed frame. Each broker `Entry` holds one connection's `SubscriptionState`: `Absent`, `ExplicitNull` or `Initialized(set)` (E64, E65); today only client controls change it.

gdaemon already depends on tokio-tungstenite 0.29 with `handshake` and axum 0.8 with `ws` (E53), so no dependency is added. `nodes/channel.rs` is the in-crate client precedent (`client_async_with_config`, E42). The public upgrade must use tungstenite types to feed the broker. `front_door/ws.rs::is_upgrade` only detects an upgrade (E68). tungstenite's `handshake::server::create_response_with_body` validates GET, HTTP/1.1 or later, `Connection: upgrade`, `Upgrade: websocket`, version 13 and a present key, and builds the 101 with the accept key; it reads only the first value of each header (E69). The upgraded stream is taken as `front_door/ws.rs::splice_over` does and wrapped with `WebSocketStream::from_raw_socket(.., Role::Server, config)`. `TokenSlot::authorize` gives the synchronous constant-time current-incarnation check, returning `NoBackend` with no live child (E48, E63), so it can run under a lock without awaiting. The supervisor publishes `BackendStatus` asynchronously (E35), so the watcher can observe a change after a replacement backend has already bound.

Front-door `authenticate` strips identity headers, passes `gobby-agent-v1.` bearers through unstamped, and stamps user, machine and key IDs plus the secret for operator keys (E37). Python's direct-listener refusal bodies are E52. Decisions 2, 3, 6, 9, 10 and 13 in the P2 framing apply.

Implementation (new symbols):

1. **`NativeWs`** (in `transport.rs`) holds the shared broker, the secret, an optional `TokenSlot`, the backend HTTP address, and one `Mutex<Option<Binding>>`, where `Binding { generation, settings, token }` records the supervisor token that proved the bind. That lock is the single commit boundary for generation state: each check of the current generation and the effect that depends on it happen while it is held, and nothing awaits while it is held.
   - Bind and publish (item 5) first re-run `TokenSlot::authorize` on the token the request presented. If it no longer passes, the request returns 403 and changes nothing: no binding, retirement, enqueue or subscription. This check and the commit share one lock hold, so a request that proved a token, stalled, and resumed after that token rotated cannot overwrite or publish into a replacement binding.
   - Bind (item 5) installs the new `Binding` and retires every registered connection of the previous generation with 1012.
   - Registration (item 2) commits only if its captured generation is still current.
   - Publication (item 5) checks the generation and enqueues into the broker.
   - Backend retirement (item 6) re-evaluates the binding it finds.
   - No second monitor or counter is added. The backend check depends only on whether the binding's token is current, so bind needs no fencing of pending or delayed `BackendStatus` changes.
2. **`NativeWs::accept(request, entry, peer)`:**
   - First validate the request head with `create_response_with_body`. Also refuse a duplicate `Upgrade`, `Connection`, `Sec-WebSocket-Key` or `Sec-WebSocket-Version` header, and a `Sec-WebSocket-Key` that is not base64 of 16 bytes. A missing or wrong version returns 426 with `Sec-WebSocket-Version: 13`; every other failure returns 400. A refused handshake opens no private leg and registers nothing. The 101 adds no extension or subprotocol header.
   - Unbound: direct returns the existing `health::unavailable` 503, and browser answers 101 then closes 1013 "WebSocket server unavailable".
   - Otherwise capture the current generation, mint a `ConnectionId`, and open the private leg to `ws://{backend}/_gobby/ws/bridge` within two seconds. gdaemon builds the private header map from scratch: each synthesized header is set exactly once, and every public header whose name matches a synthesized or allowlisted name in any letter case is dropped, never appended. Its headers are exactly `X-Gobby-Front-Door`, `X-Gobby-WS-Generation`, `X-Gobby-WS-Entry`, `X-Gobby-WS-Path`, `X-Gobby-WS-Leg` (that `ConnectionId`), the observed `X-Forwarded-For` and `X-Forwarded-Proto`, any stamped `X-Gobby-User-Id`, `X-Gobby-Machine-Id` and `X-Gobby-Key-Id`, and the public Authorization, Cookie, `X-Gobby-Break-Glass`, Origin and User-Agent.
   - Readiness: validate and consume exactly the first private frame. It must be text `bridge_ready`, version 1, with the captured generation, within two seconds. Anything else, or a timeout, fails closed exactly as unbound.
   - Direct refusal returns that status and body as an HTTP response without 101. Browser refusal answers 101 and then closes 4401 "Authentication required".
   - Admission answers 101. Once the upgraded stream is ready, registration takes the binding lock. If the captured generation is still current it registers the public sink under the entry mode and `ConnectionId`; otherwise it registers nothing and closes both sides with 1012, exactly as if the connection had been retired.
3. **Pump.**
   - Public text goes through `broker.receive`; returned application text is forwarded as text, and public binary is forwarded as binary.
   - Private text becomes a text `broker.reply` batch and private binary a binary one, so replies keep their opcode and bytes and share the connection's FIFO with broadcasts and subscription acknowledgements. Every private frame after readiness is application data.
   - Either side's close or EOF closes the other within one second with the same code (1011 when none) and calls `broker.disconnect` once.
   - The public `max_message_size` comes from the bound settings (oversize closes 1009). The private leg allows `MAX_QUEUED_BYTES`.
   - Keepalive pings every `ping_interval` seconds; no pong within `ping_timeout` closes 1011.
4. **Broker typing** (`broker.rs`).
   - A `Batch` frame is text or binary. `Batch::new(Vec<String>)` still builds text batches, and a new `Batch::binary(Bytes)` builds a one-frame binary batch. Byte accounting counts either kind, and the writer sends `Message::Text` or `Message::Binary` by kind. Filtering, ordering and overflow retirement are unchanged, and no second public writer is added.
   - `BroadcastBroker::add_subscriptions(id, events)` turns `Absent` or `ExplicitNull` into `Initialized(events)` and extends an `Initialized` set, mirroring the Python sites. It sends nothing to the client.
5. **`publish.rs`** handles POST `/_gobby/ws/bind` and `/_gobby/ws/publish`.
   - Proof: a loopback peer, the secret and the token, checked before the body is read. The token is checked again under the binding lock as part of the commit (item 1). Any proof failure returns 403 and applies nothing.
   - Bind takes a body of at most 4 KiB, mints a 128-bit random hex generation, and under the binding lock re-authorizes the presented token and installs `Binding { generation, settings, token }` with that token (item 1). It returns `{"generation"}`.
   - Publish takes `{"generation","ops":[...]}` with a body of at most 128 MiB (otherwise 413 before buffering past the bound). The whole body is validated before any operation applies: each publish operation's frames must each be at most `max_message_size` and pass `Batch::new` (otherwise 413), each subscribe operation names a leg ID and a list of event strings, and anything malformed returns 400.
   - Then, under the binding lock, a presented token that no longer passes `TokenSlot::authorize` returns 403 and a generation other than the current one returns 409, each applying nothing. Otherwise the operations apply in order (`broker.publish` once per publish operation, `broker.add_subscriptions` per subscribe operation) and the response is 204. A subscribe for a leg that is no longer registered is a no-op.
   - Python keeps one request in flight, and gdaemon applies each request before answering 204, so the broker sees Python's enqueue order.
6. **`NativeWs::retire_on_backend_change(watch::Receiver<BackendStatus>)`** takes the binding lock on every change. When `TokenSlot::authorize(Some(binding.token))` fails for the binding it finds, it clears that binding and retires its connections with 1012. A delayed change that arrives after a replacement bind finds the replacement's current token and leaves it bound, so an idle replacement keeps admitting without a publication or a manual bind.
7. **`NativeWs::shutdown()`** stops admission and closes every public connection with 1001 within one second.
8. **Version.** Bump gdaemon by `0.0.1` in `Cargo.toml`, `Cargo.lock` and `version_pins.py`. Keep `transport.rs` and `publish.rs` each under 850 lines.

**Acceptance:**

- B2.1 - The public 101 carries the correct accept key with no extension or subprotocol, and the private upgrade carries only the allowlisted headers, each synthesized header exactly once even when the public request repeats it or varies its letter case. Text and binary bytes relay exactly in both directions, binary replies included. Subscribe and unsubscribe are consumed by the broker, with acknowledgements ahead of newly selected events, and never forwarded; direct replies are unfiltered. test: `crates/gdaemon/tests/native_ws_transport.rs::native_leg_relays_frames_and_consumes_controls`.
- B2.2 - Readiness is exactly the first private frame:
  - Direct refusal returns the bridge's HTTP status and body without 101.
  - Browser refusal is 101 and then 4401.
  - A missing, malformed or wrong-generation first frame, or none within two seconds, fails closed.
  - Every later private frame, including one identical to `bridge_ready` or one naming another generation or admission result, reaches the public client unchanged and changes neither admission nor generation.
  test: `crates/gdaemon/tests/native_ws_transport.rs::bridge_readiness_is_one_time_and_entry_specific`.
- B2.3 - Bind and publish admit only a loopback peer presenting both the secret and the current token:
  - Each bind mints a fresh generation and retires the previous generation's connections with 1012.
  - A publish request applies its operations in order: each publish operation enqueues once, and each subscribe operation changes only its named leg.
  - A stale generation gets 409 and applies nothing, oversize input gets 413, a malformed body gets 400 with nothing applied, and failed proof gets 403, including a token that rotated after the request was first proven.
  test: `crates/gdaemon/tests/native_ws_transport.rs::bind_and_publish_admit_only_the_current_pair`.
- B2.4 - The A1 envelope corpus and the terminal golden corpus replay byte-identically through the native transport against a stub bridge. test: `crates/gdaemon/tests/native_ws_transport.rs::terminal_and_envelope_corpora_replay_native`.
- B2.5 - Failures are bounded:
  - Unbound entry and backend connect failures or timeouts end within two seconds.
  - An oversize public message closes 1009, and ping timeout closes 1011.
  - Private EOF closes the public side within one second, and public close closes the private side.
  - Shutdown closes 1001 within one second.
  - The broker entry count returns to zero.
  test: `crates/gdaemon/tests/native_ws_transport.rs::failures_are_bounded_and_leave_no_residue`.
- B2.6 - A subscribe operation for a live leg makes a later publish operation of that gated parametric event reach that leg only, on direct entry from `Absent` and on browser entry from the empty set. Nothing is sent to the client for the subscribe itself, a publish operation applied before the subscribe is not delivered to it, and a subscribe for an unregistered leg is a no-op. test: `crates/gdaemon/tests/native_ws_transport.rs::server_subscriptions_reach_only_the_named_leg`.
- B2.7 - The native handshake is validated before any bridge. A non-GET method, HTTP/1.0, a missing `Upgrade: websocket` or `Connection: upgrade`, a missing or wrong version (426 with `Sec-WebSocket-Version: 13`), a missing, malformed or duplicate key, and duplicate `Upgrade`, `Connection` or version headers are refused without 101; no private leg opens and nothing registers. test: `crates/gdaemon/tests/native_ws_transport.rs::invalid_handshakes_are_refused_before_the_bridge`.
- B2.8 - Generation changes commit under the binding lock, shown with deterministic barriers:
  - A rebind between readiness and registration closes that connection 1012 and leaves it unregistered.
  - A publish request for the old generation racing a rebind never reaches a new-generation connection.
  - A backend status change delivered after a replacement bind leaves the replacement bound, and an idle replacement keeps admitting new connections without a publication or a manual bind.
  - A status change whose proving token is no longer current retires that generation.
  - Stale bind after replacement: a bind proven with token A is held at a barrier after proof and before its commit; the backend token rotates to B, the status change retires A's generation, and a bind proven with B commits. Releasing the held bind returns 403, and B's binding, generation and registered connections are unchanged. A publish held the same way returns 403 and enqueues nothing.
  test: `crates/gdaemon/tests/native_ws_transport.rs::generation_changes_commit_under_the_binding_lock`.
- B2.9 - One connection's FIFO carries non-UTF-8 binary replies interleaved with text replies, broadcasts and subscription acknowledgements in enqueue order, with opcodes and bytes intact. Byte accounting and overflow retirement count binary frames. test: `crates/gdaemon/tests/ws_broker.rs::typed_batches_keep_order_and_opcode`.

**Verification (planned):** `cargo nextest run -p gobby-daemon --test native_ws_transport --test ws_broker --test ws_envelope_golden` under an RM grant and the host watcher, with in-process stub bridges, ephemeral loopback ports and synthetic secret and token. No daemon is started. The modules stay unwired until B3, so live behavior is unchanged. `tdd: true`, implementation_domain backend.

### B3 Native WS activation (depends: B1, B2) [category: code]
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: add the shared NativeWs and listener kind to FrontDoorState and dispatch reserved, application and HMR upgrades in FrontDoor::handle
- `crates/gdaemon/src/serve.rs::*` — scope-reason: one shared NativeWs across both public listeners, backend HTTP target for both, listener kind on PublicListener, backend-status watch task and shutdown drain
- `crates/gdaemon/src/state.rs::*` — scope-reason: keep the supervisor TokenSlot on RuntimeState for the private-route proof
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: retarget splice tests from /ws to the HMR path now that /ws is native
- `crates/gdaemon/tests/native_ws_transport.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: bump the managed gdaemon version pin by 0.0.1 in step with Cargo.toml
- `src/gobby/servers/websocket/server.py::*` — scope-reason: native-mode publisher attribute and start/stop calls that delegate to the publisher instead of serving the websockets listener; the file stays below 850 lines
- `src/gobby/servers/websocket/broadcast.py::*` — scope-reason: route BroadcastMixin.broadcast to the native publisher before local fanout in native mode, and skip the empty-clients returns in broadcast and broadcast_session_event in native mode
- `src/gobby/runner_init/servers.py::init_servers`
- `tests/e2e/test_native_ws_transport.py`
- `docs/contracts/native-ws-transport.md`
- `docs/contracts/parity-ledger-status.d/websocket-chat-voice-ui.json::*` — scope-reason: flip the WS transport `/ws`, `/ws/{path:path}`, subscribe and unsubscribe rows to delegated
- `docs/contracts/parity-ledger.md`

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: the mounted endpoint keeps its signature; B1 already added the bridge mount.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: keeps the init_servers re-export and signature.
- `src/gobby/runner.py` — no-edit-reason: calls init_servers with the same arguments; the publisher attaches inside it.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: restart config consumers are untouched; init_servers keeps its signature.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: without both native environment values the publisher is None and startup is unchanged.
- `tests/test_runner_lifecycle.py` — no-edit-reason: the runner initialization and lifecycle contract stays intact.
- `src/gobby/runner_lifecycle_subsystems.py` — no-edit-reason: `_start_websocket_server` already records start() failure as a subsystem failure (E51).
- `crates/gdaemon/src/front_door/ws.rs` — no-edit-reason: splice and splice_over keep serving non-application upgrades unchanged.
- `crates/gdaemon/tests/ws_golden_proxy.rs` — no-edit-reason: it replays the terminal corpus through splice_over directly, which B3 keeps.
- `tests/servers/test_ws_asgi_endpoint.py` — no-edit-reason: Python-only mode keeps browser welcome and 4401 behavior.
- `tests/servers/websocket/test_broadcast.py` — no-edit-reason: Python-only fanout is unchanged; native routing is covered by B1 and B3 tests.
- `tests/servers/test_websocket_subscriptions.py` — no-edit-reason: Python-only controls stay A1's oracle.
- `tests/terminals/test_composer_proof_events.py` — no-edit-reason: subscription selectors keep parity.

**Research context:** `FrontDoor::handle` runs `observe_peer`, then the node or backend split, then the native lease admin routes, then backend status refusal, `authenticate`, `/api/nodes/channel`, and machine routes, and finally splices every upgrade (E38). `serve_with_runtime` builds one `FrontDoorState` per `PublicListener { listener, backend }` (E47). `serve.rs::run` binds the daemon port toward backend HTTP and the WS port toward backend WS (E32). `RuntimeState` holds the `BackendStatus` receiver (E33), and `BackendConfig.write_token` is the supervisor `TokenSlot` (E34). `WebSocketServer.start` serves `websockets` with `process_request=self._authenticate` (E49) and is awaited by `_start_websocket_server`, which records failure (E51). `BroadcastMixin.broadcast` returns early with no Python clients (E44), and so does `broadcast_session_event` before it reaches `broadcast` (E60). In Python-only mode the ledger rows `WS transport /ws`, `WS transport /ws/{path:path}`, `WS message subscribe` and `WS message unsubscribe` are `proxied`, blocked on #21558; `delegated` means natively served (precedent: gmcp-stdio 1.7.8). Decisions 1 to 14 in the P2 framing apply.

Implementation:

1. **`FrontDoorState`** gains `native_ws: Arc<NativeWs>` and `ws_listener: bool`. `FrontDoorState::new` builds an unbound `NativeWs` with no `TokenSlot`, so bind is refused in tests. `serve_with_runtime` overrides both fields per listener from one shared instance, which holds the runtime `TokenSlot` (kept on `RuntimeState` by `prepare`).
2. **Dispatch in `FrontDoor::handle`**, backend door only:
   - Immediately after the node split, a `/_gobby/` path goes to the B2 handlers for POST bind or publish, or to 404 otherwise. This runs before `authenticate`, because `authenticate` strips `X-Gobby-Front-Door`.
   - At today's upgrade branch, a direct-listener upgrade on any path, or an HTTP-listener upgrade on `/ws` or `/ws/...`, calls `native_ws.accept` with the matching entry mode. Every other upgrade keeps `ws::splice`.
   - A non-upgrade request on the direct listener returns 426 with `Upgrade: websocket`.
3. **`serve.rs`** targets backend HTTP for both listeners and spawns `retire_on_backend_change` from the runtime receiver. On the shutdown signal it awaits `native_ws.shutdown()` before `serve_doors` returns.
4. **Python wiring.**
   - `init_servers` attaches `native_publisher_from_environment(websocket_config)` to `WebSocketServer.native_publisher`. Mode comes from the environment (decision 5).
   - `WebSocketServer.start` calls `publisher.start()` instead of `serve()` when the publisher exists, and raises on bind failure so the existing subsystem failure path reports it. `stop` calls `publisher.stop()`. These are the only `server.py` additions.
   - In native mode `BroadcastMixin.broadcast` hands the encoded frames to `publisher.publish` and awaits it, skipping local fanout. The native branch comes before the `self.clients` early return in `broadcast`, and `broadcast_session_event` skips its own empty-clients return in native mode.
   - Python-only mode is byte-for-byte unchanged.
5. **Proxy path.** The production application splice path for `/ws` and the direct listener is removed. No proxy fallback and no retiring verifier remains.
6. **Ledger.** Mark the four rows `delegated`, citing B3's tests, and regenerate with `uv run python scripts/generate_parity_ledger.py`. `ping` and application message rows stay `proxied`.
7. **Contract.** Write `docs/contracts/native-ws-transport.md`: entry modes and path claims, the private header allowlist and `bridge_ready` schema, bind and publish proof, status codes and limits, generation and retirement rules, close codes, credential owners, and the Orchestrator-run cutover proof (binary-set promotion through `promote_workspace_binary_set`).
8. **Version.** Bump gdaemon by `0.0.1` again.

**Acceptance:**

- B3.1 - Listener dispatch:
  - HTTP `/ws` and `/ws/x` are native browser entry, and every direct-listener path is native direct entry, sharing one `NativeWs`.
  - `/__vite_hmr` keeps the splice, and `/api/nodes/channel` is unchanged.
  - Other `/_gobby/` paths return 404 without forwarding, and non-upgrade requests on the direct listener return 426.
  test: `crates/gdaemon/tests/native_ws_transport.rs::application_paths_are_native_and_hmr_is_proxy`.
- B3.2 - Through the isolated supervised daemon:
  - Operator keys are admitted on both entries.
  - An invalid or revoked key with a valid cookie is refused at the front door, and a managed bearer with a valid cookie is denied on both entries.
  - Break-glass with a valid cookie is admitted on browser entry only, through the cookie.
  - A valid cookie alone is admitted on browser entry and refused on direct entry.
  - Forged identity, entry, path, generation, forwarded or bridge headers are ignored, including duplicated and letter-case-variant copies.
  - A public `/_gobby/ws/bridge` request gets 404, and bind or publish without proof is refused.
  test: `tests/e2e/test_native_ws_transport.py::test_credential_ownership_and_private_channel_refusal`.
- B3.3 - Native-mode Python binds from `start()` before ready with no client, serves no `websockets` listener, and has exactly one fanout owner; Python-only mode is unchanged. Killing the idle Python backend retires its generation, and the respawned backend binds a new generation and restores admission without a manual bind. A stale-token bind is refused. test: `tests/e2e/test_native_ws_transport.py::test_backend_respawn_rebinds_and_single_fanout`.
- B3.8 - Server-side subscriptions survive native mode through the isolated supervised daemon. A direct-entry client that sends only gclient's `subscribe` set and then `workspace_attach` receives a later `workspace_event` for that workspace and none for another workspace. A browser-entry client that sends `attach_to_session` receives later `session_message` and `hook_event` frames for that session. test: `tests/e2e/test_native_ws_transport.py::test_server_side_subscriptions_survive_native_mode`.
- B3.4 - Reconnect after a backend restart starts from the entry mode's initial subscription state. Message limits, backend failure and daemon stop are bounded (public close 1001 within one second), and no client, handler or lease residue remains. test: `tests/e2e/test_native_ws_transport.py::test_failure_reconnect_and_shutdown_are_bounded`.
- B3.5 - Native gterm direct access and a running terminal survive daemon loss and restart, independently of the native WS listener. test: `tests/e2e/test_native_ws_transport.py::test_gterm_remains_available_without_daemon`.
- B3.6 - The contract documents entry modes, the public and private frame boundary, credential owners, proofs, limits, failure semantics and the cutover proof, and states that no retired verifier or application proxy fallback remains. file: `docs/contracts/native-ws-transport.md`.
- B3.7 - The four transport and control rows are `delegated`, and the committed ledger equals its regeneration. test: `tests/test_parity_ledger.py::test_committed_ledger_matches_current_public_inventory`.

**Verification (planned):**
- `cargo nextest run -p gobby-daemon --test native_ws_transport --test front_door` (RM grant and host watcher).
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_native_ws_transport.py tests/servers/test_native_ws_bridge.py tests/servers/test_native_ws_publish.py tests/servers/test_ws_asgi_endpoint.py tests/servers/test_app_factory_vite_proxy.py tests/servers/test_websocket_subscriptions.py tests/terminals/test_composer_proof_events.py tests/test_parity_ledger.py tests/test_runner_backend.py` (RM START and DONE).
- The e2e tests use `tests/e2e/conftest.py`'s isolated `daemon_instance` with temporary home, test hub and ephemeral ports. They never read operator token or bootstrap files and never contact the running daemon. No full pytest run.
- Production activation is the Orchestrator's announced cutover; this leaf runs none.
- `tdd: true`, implementation_domain fullstack.

## D1 Auth-integrated private bridge and native WS activation (depends: A1, A2, A3)
`kind: deferred`

```yaml
deferral:
  task_ref: "#23881"
  reason: "The native upgrade/auth seam is owned by #23519 and its live activation by the separate #23523. This tail requires both external tasks plus A1/A2/A3 before implementation; an isolated source landing alone does not establish activated auth. The fixed spec below introduces no fallback retiring verifier."
  owner: "Lane 8 Terminal port, coordinated with Lane 1"
  original_acceptance_items:
    - D1.1
    - D1.2
    - D1.3
    - D1.4
    - D1.5
    - D1.6
    - D1.7
    - D1.8
```

At expansion, the Orchestrator creates this planning tail under #21558 with `needs-planning` and `deferred-from:native-ws-transport:D1`, then records explicit blocked-by edges to #23519, #23523 and the A1/A2/A3 implementation leaves. Verify the resulting dependency rows; neither a prose wait nor a manifest external edge suffices. Replace the placeholder with that real task ref and validate/hash again. Before implementing the tail, verify #23519's landed interface and #23523's separate completed live-activation receipt, materialize this fixed specification as implementation leaf coverage, and obtain normal plan approval. The native-WS production cutover still belongs to the Orchestrator; #23523 activates auth, not this WS implementation. Do not create a parallel authentication plan or drop either external edge.

Materialized 2026-10-16 as P2 (B1, B2, B3) from the #23881 spec, re-anchored at 0.5.0 `7ba471a18c`. Implementation Targets, research context, decisions and tests live in those sections; the obligations below remain this deferral's coverage record and map to them: D1.1 -> B2.7, B3.1; D1.2 -> B1.1, B1.2, B3.2; D1.3 -> B1.3, B2.2, B2.9; D1.4 -> B1.4, B1.5, B1.6, B1.7, B1.8, B2.3, B2.6, B2.8, B3.3, B3.8; D1.5 -> B2.4; D1.6 -> B2.5, B3.4; D1.7 -> B3.5; D1.8 -> B3.6, B3.7. #23881 closes when B1 to B3 close.

Obligations retained for the deferred task:

- D1.1 - Both application entry points use native Rust upgrade/lifecycle and one broker; HMR remains proxied. test: `crates/gdaemon/tests/native_ws_transport.rs::application_paths_are_native_and_hmr_is_proxy`.
- D1.2 - Operator key verification has only the #23519 owner; managed admission/cookie policy retains original WS context. Both entry modes cover managed+valid cookie (deny), operator identity+cookie (operator admission), break-glass+cookie (browser cookie admission only), rejected key+cookie (deny), cookie-only entry differences, forged peer/path/mode/identity and missing/stale pair proof. test: `tests/e2e/test_native_ws_transport.py::test_credential_ownership_and_private_channel_refusal`.
- D1.3 - Welcome, direct replies, text/binary bytes and once-only cleanup remain exact. Private readiness is exactly the first private frame: a missing, malformed, wrong-generation or late first frame fails closed, every later private frame passes through as opaque application data (including one identical to readiness) without changing admission or generation, and no private controls leak. test: `tests/servers/test_native_ws_bridge.py::test_bridge_preserves_application_connection_lifecycle`.
- D1.4 - Native fanout occurs once, direct replies bypass filtering and ambiguous publication never retries. A blackholed HTTP publish channel retires only the affected generation through local bridge-leg closure within the stated bound, including an old/new generation race. Initial binding without any clients succeeds after HTTP startup; failed binding keeps admission closed, and child respawn requires a distinct authenticated generation. An isolated idle-child crash with no clients or publications must drive the real monitor notification and restore admission without a manually invoked bind. Failed rebind, crash during initial bind, duplicate/stale notification and shutdown races must remain closed or preserve only the current generation. test: `tests/servers/test_native_ws_publish.py::test_single_fanout_owner_and_ambiguous_publish`.
- D1.5 - Both runtimes replay the shared event corpus and the existing terminal corpus remains byte-identical through native transport. test: `crates/gdaemon/tests/native_ws_transport.rs::terminal_and_envelope_corpora_replay_native`.
- D1.6 - Message limits, ping/close, backpressure, backend failure, reconnect and shutdown are bounded and leave no client/handler/lease residue. test: `tests/e2e/test_native_ws_transport.py::test_failure_reconnect_and_shutdown_are_bounded`.
- D1.7 - Native gterm direct access and a running terminal survive daemon loss/restart independently of the new WS listener. test: `tests/e2e/test_native_ws_transport.py::test_gterm_remains_available_without_daemon`.
- D1.8 - The public/internal frame boundary, credential owners, limits, failure semantics and cutover proof are documented; no retired verifier or application proxy fallback remains active. file: `docs/contracts/native-ws-transport.md`.

## V1 Plan Changelog
`kind: framing`

2026-10-06: Initial narrative draft by gobby#15382 on LM7's authorized writer loan. Transport-only ownership and #23519 auth boundaries follow the Orchestrator's 18:42 CT ruling. Adv2 pre-audit and consensus are pending. No M1 has been authored.

2026-10-06: Adv2 audit `0c6cbba0-4090-4506-9a20-73f423365787` verified all initial source quotes and found four blockers. Entry-mode initialization and absent/null transitions are now explicit; malformed native members/types are validated atomically outside the unchanged Python parity domain; bridge admission carries original credential/scope context with a two-entry test matrix; and D1 is externally gated by both #23519 source and #23523 live activation. Readiness framing, a local bridge-leg retirement actuator, and baseline unchanged consumers address the advisory details. Re-audit and consensus remain pending; no M1 or main write.

2026-10-06: Adv2 re-audit `96eb07e9-d046-4163-9297-6887cd630263` cleared WS-A2-01 through WS-A2-04 and identified WS-A2-05: no production trigger for idle child rebind. The existing respawn monitor now owns a bounded async replacement notification, registered by run_gobby to the publisher with current-incarnation ordering, startup/shutdown race handling and surfaced failed-bind behavior. Integration targets and acceptance coverage include the real idle-respawn path. Re-audit/consensus remain pending.

2026-10-06: Pre-audit consensus reached with Adv2 gobby#15414, receipt `43c0f7fa-a836-4109-8ca5-b34fc4391e07`, on behavior, decisions, all 13 active criteria and eight deferred obligations. WS-A2-01 through WS-A2-05 are resolved; Adv2 independently verified all 84 quoted source lines (per Adv2’s packet correction) and base validation passed. WS-A2-06 inventory nit is corrected with an explicit whole-file scope reason for the existing runner/child test harness. Final-byte check remains pending. M1, canonical GO, Program Director review, Josh approval and expansion remain separate gates; none is implied by pre-audit consensus.

2026-10-08: Refresh by W4 gobby#15528 on LM7's direction: the Downstream paragraph in C1 names what unblocks #21562, #21568 and #23219; E1 is re-cited at 0.5.0 `3987d09dcc4311a1ebda7d58a47d885a9413560a` with #23519's landed auth surface in E2, E14 and E22; the status line is current. No acceptance item changed. Plan Adversary gobby#15401 reviews this delta.

2026-10-09: N1 repair by Plan Writer gobby#15860 on LM7's assignment. Adv1 gobby#15401's delta review of 41884d6ebc passed with one nit, N1: the C1 Downstream paragraph overstated readiness. It now says D1's activation and the closure of #21558 remove the WS prerequisite from #21562, #21568 and #23219, while #21560 still blocks #21562 and #21568, #23214, #23216, #23217 and #23218 still block #23219, and #23219 keeps its needs-planning hold. "A1 to A3 alone unblock none" is preserved. The status line records Adv1's PASS. No acceptance item, Target or M1 entry changed. Adv1 rechecks N1.

2026-10-16: #23881 spec by Lane 8 developer gobby#16915 on the Orchestrator's assignment via LM8. Gates confirmed: A1 #23878, A2 #23879, A3 #23880, #23519 and #23523 closed. Re-anchoring found #23457 (`fde8c6182d`) deleted `src/gobby/runner_front_door.py` and inverted process ownership (gdaemon supervises Python), so E27 to E30 are retired and the old item 8 monitor machinery is replaced by per-incarnation bind with supervisor-token proof (P2 decisions 1 to 3). #24444's route families add no WS family (decision 4). The private leg drops the JSON outer record and base64 in favour of one-to-one WebSocket framing after a single first-frame `bridge_ready` (decision 6). gdaemon already carries tokio-tungstenite 0.29 (E53), superseding E16's 0.26. D1's eight obligations are kept and mapped to B1 to B3; the parity ledger rows are a new Target. LM1 gobby#15990 agreed to the auth seam and pair proof. Security gobby#16011 and Lane 8 reviewer gobby#16912 review. Plan writer gobby#16490 lands each revision in the main checkout; no M1 derivation, expansion or restart has occurred.

## E1 Source evidence
`kind: framing`

The following are hash-verified `gcode evidence` reads on the main checkout at 0.5.0 `3987d09dcc4311a1ebda7d58a47d885a9413560a` (re-read 2026-10-08), complete with no warnings. Every quote uses the returned numbered excerpt. Symbol entries cite the symbol read; path-only entries cite the range from the first to the last quoted line. #23519's landed front-door cutover shows in E2, E14 and E22.

- E1 — `crates/gdaemon/src/serve.rs::run`, excerpt `44a3b68d5a4b4afcae96d4b1eac80bbdb651fa440d3299b2b37c13e4f4f775a4`: `115| let mut listeners = bind(host, bootstrap.daemon_port, backend_http).await?;` and `116| listeners.extend(bind(host, bootstrap.websocket_port, backend_ws).await?);`.
- E2 — `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`, excerpt `a0a35c375da2dce99a3ff2457c002d018856ea017a03061c38745adb3add8ceb`: `79| if let Err(response) = self.state.auth.authenticate(request.headers_mut()).await {` authenticates every request first (#23519), then `82| if ws::is_upgrade(request.headers()) {` and `83| ws::splice(&self.state, request).await`.
- E3 — `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._handle_subscribe`, excerpt `9de81f0f8df694bb3b904aa80caa5dd153c0b6dfc5269f5e87303430f94ec3bd`: `189| websocket.subscriptions.update(events)` and `196| "events": list(websocket.subscriptions),`.
- E4 — `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._handle_unsubscribe`, excerpt `a4aed7ed4bac277dbe6f28071917e5fa68d3926cd8054046bf560bf16acca369`: `217| if not events or "*" in events:` and `218| current_subscriptions.clear()`.
- E5 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin._is_subscribed`, excerpt `ec3ddde0936b6a6afcb72c47909ee6129f6f4ee90af8ff25c1bfb39360b29eb7`: `83| if subs is None:` / `84| return False`; `116| if msg_type not in event_types:` / `117| return True`; `133| if message.get(key) == value:` / `134| return True`.
- E6 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast`, excerpt `72f3b2ac1de123c1b2b14b9c645ef7be72270320c99cea28fba82f2301b55902`: `193| payloads = [json_dumps(frame) for frame in frames] or [json_dumps(message)]`, `201| if self._is_subscribed(websocket, message):`, and `209| *(self._send_payloads(websocket, payloads) for websocket in recipients)`.
- E7 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin._send_broadcast`, excerpt `916d100eb1729547a3cb7cec1f586d491c2114ecdfc30263e4d6d9c0676543b5`: `149| timeout=BROADCAST_SEND_TIMEOUT_SECONDS,`, `159| websocket.close(code=1011, reason="Broadcast send timed out"),`, `160| timeout=BROADCAST_CLOSE_TIMEOUT_SECONDS,`.
- E8 — `src/gobby/servers/websocket/broadcast.py`, excerpt `45a2f146f5fd48649924236fbfdc0c61bf9ac9fc80bbfbcc4bcea76f0ca81f92`: `35| BROADCAST_SEND_TIMEOUT_SECONDS = 2.0` and `36| BROADCAST_CLOSE_TIMEOUT_SECONDS = 1.0`.
- E9 — `crates/gdaemon/tests/ws_golden_proxy.rs::corpus`, excerpt `0f0b7e41a02c5662a2456148fcfe324a0f908242f1f920684388baa50e30c00c`: `14| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/terminal_ws_golden");`, `16| &std::fs::read(dir.join("manifest.json")).expect("read golden manifest"),`, and `25| let bytes = std::fs::read(dir.join(&name)).expect("read fixture");`.
- E10 — `src/gobby/servers/websocket/models.py::WebSocketConfig`, excerpt `60182f482fe10776d2cb8a8a708e33bc66a268b1ef1bda6604b3c4f2f493c8a5`: `40| ping_interval: int = 30  # seconds`, `41| ping_timeout: int = 10  # seconds`, and `42| max_message_size: int = 5 * 1024 * 1024  # 5MB (increased for voice audio)`.
- E11 — `src/gobby/servers/websocket/server.py`, excerpt `d5b62570dfcffa924690ec347791254fa7bc351606b3d2456f909ef53f8ef806`: `519| "subscribe": self._handle_subscribe,`, `520| "unsubscribe": self._handle_unsubscribe,`, `522| "terminal_input": TerminalWsMixin._handle_terminal_input.__get__(self),`, and `523| "chat_message": self._handle_chat_message,`.
- E15 — `src/gobby/servers/websocket/server.py::WebSocketServer.start`, excerpt `c6893053d5da62735866bdb7d8e6bd5f772bc416ee91d6b1e2411a312e11e79f`: `691| process_request=self._authenticate,`, `692| ping_interval=self.config.ping_interval,`, `693| ping_timeout=self.config.ping_timeout,`, `694| max_size=self.config.max_message_size,`, and `695| compression="deflate",`.
- E12 — `src/gobby/servers/websocket/server.py::WebSocketServer._handle_connection`, excerpt `af9ab343adac977985d1a029fa27bc707d03595a7c141eda0e4fffd7cdac1592`: `415| client_id = str(uuid4())`, `439| "type": "connection_established",`, `476| await self._cancel_off_loop(websocket)`, `477| await self._cleanup_terminal_client(websocket)`.
- E13 — `src/gobby/servers/_app_ui.py::_mount_ws_endpoint`, excerpt `c2279c93d1ad91b5cbb4a55830a0b36094770054bb703275bf4ec7bd1664db5d`: `55| await adapter.accept()`, `62| server.auth_service.is_request_authenticated,`, `66| await adapter.close(code=4401, reason="Authentication required")`.
- E14 — `src/gobby/servers/websocket/auth.py::AuthMixin._authenticate`, excerpt `6801c26a7fba0bb3b3629d031283ba12caf6c35fc50efacd4b558339755e670d`: `38| # The native front door validates the key and forwards its bound identity.`, `66| user_id = await self.auth_callback(request.headers)`, `73| 403,`. #23519 landed this forwarded-identity check on the direct listener; it is not a Python key verifier, and D1 adds none.
- E16 — `crates/gclient/Cargo.toml`, excerpt `46b5b91158356c5c9ed9792144cf460fda0ee75bdcd9556d09256fa20b5b9191`: `38| tokio-tungstenite = { version = "0.26", default-features = false, features = ["connect", "rustls-tls-webpki-roots"] }`.
- E17 — `crates/gclient/src/daemon/live_reader.rs`, excerpt `0f023f5ea2d8dd63bfca4fb36e2f254993194d0a71e98fbd21be648c2e3d2fdb`: `21| const MAX_ASSEMBLY_BYTES: usize = 16 * 1024 * 1024;` and `22| const MAX_AGGREGATE_BYTES: usize = 64 * 1024 * 1024;`.
- E18 — `crates/gclient/src/daemon/live_reader.rs`, excerpt `7f7d666dd5eb3d35c6a260df4b928d5446f2b5448b380499a3652e0bce942227`: `98| url.set_path("/ws");` and `103| request.headers_mut().insert(AUTHORIZATION, authorization);`.
- E19 — `crates/gdaemon/src/front_door/ws.rs::splice`, excerpt `5696605731859336dd111e4a001921773c84e0e203cda5327bedb4a5d3d141ce`: `46| let mut response = match sender.send_request(Request::from_parts(parts, body)).await {` and `63| let _ = tokio::io::copy_bidirectional(&mut client, &mut backend).await;`.
- E20 — `src/gobby/servers/websocket/asgi_adapter.py::ASGIWebSocketAdapter.__init__`, excerpt `1c7e663e90d6e0671f7bba9272ca99490efc2eb5cd418b58190e26afbacc6774`: `20| self.subscriptions: set[str] = set()`.
- E21 — `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._send_error`, excerpt `2752405e9ed79f3edd04c14fa253b9d2459a48785f2266cc78465160b7cf268d`: `39| code: str = "ERROR",`, `51| "type": "error",`, `52| "code": code,`, `53| "message": message,`.
- E22 — `src/gobby/servers/auth_service.py`, excerpt `6ad06994c7a8b554d421511460251b4b2e3d0b7cc0fb22b30921dca1900ceda4`: `362| if self.verified_front_door_identity(request) is not None:`, `364| authorization = request.headers.get("Authorization")`, `370| claims = self._classify_agent_token(request, parts[1])`, `371| return None if isinstance(claims, AgentApiTokenClaims) else claims`, `373| session_token = request.cookies.get(_SESSION_COOKIE)`, and `405| if request.scope.get("type") != "http" or request.client is None:` / `406| return False`. The verified front-door identity now precedes Authorization, which precedes the cookie.
- E23 — `src/gobby/servers/auth_service.py` complete capability matrix, excerpt `eac0a071b0970236f2a63d1a49ce824bc70e01e6c16ccba138abfa6402b04fd9`: `84| _AGENT_CAPABILITY_MATRIX: tuple[_AgentRoute, ...] = (` through `127| )` lists API routes only, with no WS route. E24 supplies the fail-closed result.
- E24 — `src/gobby/servers/auth_service.py::AuthService._classify_agent_token`, excerpt `269255ec18097be3f9c330a6267befbce44fc98ae6ad3c79505777e7cb355f4a`: `452| entry = _agent_capability_allows(request)`, `453| if entry is None:`, and `454| return "route_not_permitted"`.

- E25 — `src/gobby/config/bootstrap.py::FrontDoorConfig`, excerpt `c897fb8ea7dca6eb85ef28d49eceb6ac9cda3167242803b74d787f6f55d9cec6`: `91| enabled: bool = True` anchors the existing mode choice.
- E26 — `src/gobby/runner_lifecycle.py::run_daemon` lines 270-277 (the whole symbol exceeds the default evidence size), excerpt `c63d9174d5b604f18209e23a08298f30497f3315cb30504e27a8ef29f74a323b`: `270| while not server.started and not server_task.done() and not runner._shutdown_requested:` waits for HTTP; `273| if server.started and not runner._shutdown_requested:` gates `277| runner._subsystem_init_task = asyncio.create_task(`. No lifecycle reordering is required.

- E27 — `src/gobby/runner_front_door.py::FrontDoorChild._watch`, excerpt `12a393d2cc9df97ef59b797297b6a754b9e1cfb2fe85a8676fdfb3a410f983ae`: `293| await asyncio.to_thread(self.start)` replaces only the Rust child; `299| serving_since = time.monotonic()` marks successful replacement. This existing monitor owns the new async publisher notification.
- E28 — `src/gobby/runner_front_door.py::FrontDoorChild.disarm`, excerpt `2524278860618500798ffba2982cc3437155fba792cfe615a3dc54f37add1d0f`: `154| self._disarmed = True` and `157| monitor.get_loop().call_soon_threadsafe(monitor.cancel)` anchor owning-loop shutdown cancellation.
- E29 — `src/gobby/runner.py::run_gobby`, excerpt `65e4106e84f6a189659d1cf182568e9548f788fa3901d2865a4088986ade60b3`: `498| runner = await GobbyRunner.create(config_path=config_path, verbose=verbose)` and `501| active.front_door_child = front_door` anchor publisher/child wiring before run.
- E30 — `src/gobby/runner_front_door.py::FrontDoorChild.start`, excerpt `d7fe7c5cc571a1ea4405cd3071430e5fe1eefad06894db71d103820cf8fe6b14`: `133| self._spawn()` and `135| self._wait_until_serving()` establish a successfully serving child incarnation.
- E31 — `src/gobby/runner_lifecycle_subsystems.py::_start_websocket_server`, excerpt `3c271e658b5a8562acd3a23e5087c70ad5fb22020aacf03303f2c32c4dd6cfd7`: `593| runner._websocket_task = asyncio.create_task(` and `594| runner.websocket_server.start(), name="websocket-server"` anchor one initial WS startup, not a child-respawn trigger.

Re-anchor 2026-10-16 (gobby#16915) at 0.5.0 `7ba471a18c4d547c18114fda21adb30acb0eb1d9` through `gcode evidence` on the lane-8 worktree (byte-identical to that commit). E27, E28 and E30 are retired: `src/gobby/runner_front_door.py` was deleted by `fde8c6182d` (#23457). E29's FrontDoorChild wiring no longer exists. E2 is superseded by E38, E16 by E53 and E22 by E39 to E41.

- E32 — `crates/gdaemon/src/serve.rs` lines 135-240, excerpt `d149840d511310b0d2f3cb5db8b81d55867b053fe96c28003ec6a40f90945e3b`: `136| rand::thread_rng().fill_bytes(&mut secret);`, `211| crate::state::RuntimeState::prepare(&bootstrap, home, &secret, pool.clone())?`, `236| let mut listeners = bind(&bootstrap.bind_host, bootstrap.daemon_port, backend_http).await?;`, `237| listeners.extend(bind(&bootstrap.bind_host, bootstrap.websocket_port, backend_ws).await?);`.
- E33 — `crates/gdaemon/src/state.rs::RuntimeState::prepare`, excerpt `46b37e09f102e097145b08e175476845132bd328ea4dca252ef5565404bee749`: `107| .insert("GOBBY_FRONT_DOOR_SECRET".into(), secret.into());` and `123| backend: supervisor.subscribe(),`.
- E34 — `crates/gdaemon/src/lifecycle/backend.rs::BackendChild::spawn`, excerpt `b0563ec0b142578225dfb932b29045be9a04d5397ce7817a6ce03a6ad3b87e24`: `348| rand::thread_rng().fill_bytes(&mut token);`, `351| let write_token = config.write_token.install(token.clone());`, `352| match (&writer).write_all(format!("{token}\n").as_bytes()) {`.
- E35 — `crates/gdaemon/src/lifecycle/backend.rs::BackendSupervisor::supervise`, excerpt `35c61d95e8201fe82f505cad4dfbe74cfa22d7ad97c1adb36351b2a0020047f4`: `194| let mut backend = BackendChild::spawn(&self.config)?;`, `197| self.state.send_replace(BackendStatus::Starting { pid });`, `208| status = backend.child.wait() => break status?,`, `262| self.state.send_replace(BackendStatus::Backoff { delay });`.
- E36 — `src/gobby/runner_backend.py::watch_supervisor`, excerpt `65431cd83b441c57ec5318e9bb1ca97c98f58bceb30070a5057168f6423956ed`: `94| value = os.environ.get("GOBBY_SUPERVISOR_FD")` and `136| on_eof()`.
- E37 — `crates/gdaemon/src/front_door/auth.rs::AuthState::authenticate`, excerpt `3d0de92eedf12c7e483b4f09c799de1f7ae9add649fc584f6cf205386e0cdf0d`: `117| headers.remove(name);`, `130| if bearer.starts_with("gobby-agent-v1.") {`, `144| self.secret.as_str(),`.
- E38 — `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`, excerpt `cf8ccd687896735079dbbd54228c62dfd416955bb7d82100c6c154bc5d9d27d6`: `105| observe_peer(request.headers_mut(), peer, https);`, `123| node.relay.splice(request, *ws_listener).await`, `134| if crate::lease::admin_api::is_route(request.uri().path()) {`, `181| if let Err(response) = backend_state.auth.authenticate(request.headers_mut()).await {`, `184| if request.uri().path() == "/api/nodes/channel"`, `192| if ws::is_upgrade(request.headers()) {`, `193| ws::splice(&state, request).await`.
- E39 — `src/gobby/servers/auth_service.py::AuthService._legacy_rejection`, excerpt `48440f857e5f7cc0ed3a7099c327ed44dd4a98c77e7bc43dc01f35d87962a926`: `340| if self.break_glass_admits(request):`, `342| if self.verified_front_door_identity(request) is not None:`, `350| claims = self._classify_agent_token(request, parts[1])`, `353| session_token = request.cookies.get(_SESSION_COOKIE)`.
- E40 — `src/gobby/servers/auth_service.py::AuthService.break_glass_admits`, excerpt `5bf3690b982536697aaf350db307dd84734dcf9a1b61f220aef08ad807d158d8`: `385| if request.scope.get("type") != "http" or request.client is None:` and `387| if not is_loopback_host(request.client.host):`.
- E41 — `src/gobby/servers/auth_service.py::verified_front_door_identity`, excerpt `48164dbe61c11d85aa37ee017da4eefe81ca8340f170944b9cc06b23c86fe583`: `197| if not secrets.compare_digest(supplied.encode(), secret.encode()):` and `202| if not user_id or not machine_id or not key_id:`.
- E42 — `crates/gdaemon/src/nodes/channel.rs` lines 300-360, excerpt `5ab4d3ceded594d0488168bc7ac6d945d81a65558f35b56025fd61b7b8aea12d`: `349| tokio_tungstenite::client_async_with_config(request, stream, None).await?;`.
- E43 — `crates/gdaemon/src/websocket/broker.rs::BroadcastBroker::publish`, excerpt `e9352eddbf09e7f5810b40fb4e6c8a6fec0973e88014b275cdaa02244971d6a5`: `277| .filter(|(_, entry)| entry.state.matches_broadcast(filter) && !entry.link.push(batch))` and `281| retire(&mut entries, id, Retirement::Overflow);`.
- E44 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast`, excerpt `72f3b2ac1de123c1b2b14b9c645ef7be72270320c99cea28fba82f2301b55902`: `189| if not self.clients:`, `193| payloads = [json_dumps(frame) for frame in frames] or [json_dumps(message)]`, `201| if self._is_subscribed(websocket, message):`.
- E45 — `src/gobby/servers/_app_ui.py::_mount_vite_hmr_proxy`, excerpt `7e17b610326428f6c06bba728b917563aea127488feb5dc0ec4062b140109844`: `178| @app.websocket("/__vite_hmr")` and `182| @app.websocket("/__vite_hmr/{path:path}")`.
- E46 — `src/gobby/runner_backend.py::backend_bind`, excerpt `71f6a60e293a31125b7e855b46f374643bd4de0c7d5e21de8710d4e7c60c7450`: `33| if bootstrap.front_door.enabled or "GOBBY_SUPERVISOR_FD" in os.environ:` and `34| http, ws = backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)`.
- E47 — `crates/gdaemon/src/serve.rs::serve_with_runtime`, excerpt `bc84e04476bcad7ff462db6780088ac0d81eab8307d03ff3a1f4a3ee7119b789`: `426| for PublicListener { listener, backend } in listeners {`, `432| ..FrontDoorState::new(backend, BackendState::Down, auth.clone())`, `434| let table = RouteTable::new(FAMILIES, routes, &state);`.
- E48 — `crates/gterminals/src/write_api.rs::TokenSlot::authorize`, excerpt `5d92ac7d3f8eb2ff10972c4e1dd7682068886191fcde41592b78002aead1185f`: `39| let current = current.as_deref().ok_or(WriteRefused::NoBackend)?;` and `42| Some(presented) if constant_time_eq(presented.as_bytes(), current.as_bytes()) => Ok(()),`.
- E49 — `src/gobby/servers/websocket/server.py::WebSocketServer.start`, excerpt `c6893053d5da62735866bdb7d8e6bd5f772bc416ee91d6b1e2411a312e11e79f`: `711| self._server = await serve(`, `715| process_request=self._authenticate,`, `719| compression="deflate",`.
- E50 — `src/gobby/servers/_app_ui.py::_mount_ws_endpoint`, excerpt `c2279c93d1ad91b5cbb4a55830a0b36094770054bb703275bf4ec7bd1664db5d`: `55| await adapter.accept()`, `58| await adapter.close(code=1013, reason="WebSocket server unavailable")`, `62| server.auth_service.is_request_authenticated,`, `66| await adapter.close(code=4401, reason="Authentication required")`.
- E51 — `src/gobby/runner_lifecycle_subsystems.py::_start_websocket_server`, excerpt `3c271e658b5a8562acd3a23e5087c70ad5fb22020aacf03303f2c32c4dd6cfd7`: `609| runner.websocket_server.start(), name="websocket-server"` and `612| lambda task: _record_websocket_startup_result(task, tracker)`.
- E52 — `src/gobby/servers/websocket/auth.py::AuthMixin._authenticate`, excerpt `6801c26a7fba0bb3b3629d031283ba12caf6c35fc50efacd4b558339755e670d`: `50| b"Unauthorized: Missing Authorization header\n",`, `62| b"Unauthorized: Expected Bearer token\n",`, `76| b"Forbidden: Invalid token\n",`.
- E53 — `crates/gdaemon/Cargo.toml` lines 20-30, excerpt `afc1953cd0ad6f9a7d9fcd9b0fd95c9bf764fb18811a99bfd82c68296d782f33`: `24| axum = { version = "0.8", default-features = false, features = ["ws"] }` and `26| tokio-tungstenite = { version = "0.29", default-features = false, features = ["handshake"] }`.
- E54 — `src/gobby/servers/websocket/workspace_ws.py::WorkspaceWsMixin._handle_workspace_attach`, excerpt `ab5999c1d24d2011484aa343474deffce5ed381646d9c1c12ea6091cc65b3e8c`: `179| websocket.subscriptions.add(f"workspace_event:workspace_id={snapshot.workspace.id}")` and `182| await self._send_json(websocket, reply)`.
- E55 — `src/gobby/servers/websocket/workspace_ws.py::WorkspaceWsMixin._read_attach_snapshot`, excerpt `1125897fb3603b4767fb36748e8d42104f59812b275c9783ba82c3d3ffdd1ece`: `280| # Register every client before yielding after the snapshot fence opens.`, `285| websocket.subscriptions.add(f"workspace_event:workspace_id={snapshot.workspace.id}")`, `290| seq = cast(int, self._leases().lifecycle_snapshot()["seq"])`.
- E56 — `src/gobby/servers/websocket/handlers/session_observe_proxy.py::handle_attach_to_session`, excerpt `a4368259d4c5c5bd16cb77118f380076cdf0d6f15616f39771475364fb597695`: `171| websocket.subscriptions.add(f"session_message:session_id={session_id}")` and `172| websocket.subscriptions.add(f"hook_event:session_id={session.external_id}")`.
- E57 — `crates/gclient/src/daemon/live.rs::SUBSCRIBED_EVENTS`, excerpt `20a72fa18209f6ef0976ab58172588eec7463e855eda9292af5ba605b6a8b953`: `32| pub const SUBSCRIBED_EVENTS: [&str; 5] = [` listing terminal, agent, project, worktree and session events.
- E58 — `crates/gclient/src/daemon/live_reader.rs` lines 319-321, excerpt `22aac8cec3997515c66c670f14f94dd8964078ee920f97a8c70828c04508090e`: `319| // Only the attached workspace's parametric subscription should deliver`.
- E59 — `crates/gclient/tests/daemon_live.rs` line 2513, excerpt `c1723dfcf8bf18d5ce441444d811d785d434637b5d13373ddfd0c33bc3b5b997`: `2513| assert!(!SUBSCRIBED_EVENTS.contains(&"workspace_event"));`.
- E60 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast_session_event`, excerpt `16b97c7dd2276416f77b0fe1f82c3f1fda4f714364c577346fcd67eb79cf35aa`: `244| if not self.clients:` and `245| return`.
- E61 — `src/gobby/runner.py` lines 446-467, excerpt `6fe79ae612cb2624abb0445558d3eb685fb98648ba44c3dd6fca31dff2aeb01b`: `446| watcher = asyncio.create_task(watch_supervisor(supervisor_gone), name="supervisor-liveness")` and `467| runner = await GobbyRunner.create(config_path=config_path, verbose=verbose)`.
- E62 — `src/gobby/runner_backend.py::supervisor_write_token`, excerpt `669439c0f9c00ff26f0f86c70146a06516c92450a86de750378728f9bc4f2529`: `84| _token = _TokenLine()` and `89| return _token.value`.
- E63 — `crates/gterminals/src/write_api.rs::TokenSlot::authorize`, excerpt `0d871cdeb8cf9798476282084062f52b11d7ad2395bd3b4e0fbab3cb71b0f68e`: `39| let current = current.as_deref().ok_or(WriteRefused::NoBackend)?;` and `42| Some(presented) if constant_time_eq(presented.as_bytes(), current.as_bytes()) => Ok(()),`.
- E64 — `crates/gdaemon/src/websocket/subscriptions.rs::SubscriptionState`, excerpt `d905d11e0fa8e905bbb322b450a35bb4654636b687458838226c145394df26f5`: `65| Absent,`, `68| ExplicitNull,`, `70| Initialized(BTreeSet<String>),`.
- E65 — `crates/gdaemon/src/websocket/broker.rs::Entry`, excerpt `57562fbbb2785408f69e1ac47963667bc9afb4f285ae424ed65f79ebdf97c3af`: `185| state: SubscriptionState,` and `186| link: Arc<Link>,`.
- E66 — `src/gobby/runner_broadcasting.py` lines 213-220, excerpt `8abb7aadecadbeec9940ff3c2b41eaf98f6eaaf13765aec666b5ca934e8e67b8`: `213| async def broadcast_terminal_output(run_id: str, data: str) -> None:` and `220| pty_manager.set_output_callback(broadcast_terminal_output)`.
- E67 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast_terminal_output`, excerpt `299b1292b6fd8145e0d0475abb38ee4f62d4e0718298040979c038f5af0284f0`: `533| "type": "terminal_output",` and `539| await self.broadcast(message)`.
- E68 — `crates/gdaemon/src/front_door/ws.rs::is_upgrade`, excerpt `a3d6011f03314f4577ad5399fcda4173289feab117af9fc651149468ed9cdb63`: `17| headers.contains_key(UPGRADE)` and `23| .any(|token| token.trim().eq_ignore_ascii_case("upgrade"))`.
- E69 — tungstenite 0.29.0 `src/handshake/server.rs` lines 36-80 (Cargo registry source, not in the code index; sha256 of the excerpt `15cc690ee57cd6b2d5ff59bc0f3f9479f708714dbc89751093b24f5bdadf2bf2`): `create_parts` refuses a non-GET method, a version below HTTP/1.1, a missing `Connection: Upgrade`, a missing `Upgrade: websocket`, a version other than 13 and a missing key, reading each with `headers().get`, then builds the 101 with `derive_accept_key`.
- E70 — `crates/gdaemon/src/websocket/broker.rs::Batch` and `send_batch`, excerpts `f803f5f4bd0b704fbcf6131f09c56a7c5edf418a904c16e4f965d8ae66fa794a` and `6ddb940110f20c1a5b826544117237ab6e315acaf285e976970e82c3ece3a3cf`: `53| frames: Arc<[Utf8Bytes]>,`, `62| pub fn new(frames: Vec<String>) -> Result<Self, BatchTooLarge> {` and `363| sent = timeout(SEND_TIMEOUT, sink.send(Message::Text(frame.clone()))) => sent,`.

## Q1 Planning validation
`kind: verification`

Observed: task/parent read, source inspection, consumer literal sweeps and a successful project-aware base validation on the materialized narrative. Command: `uv run gobby plans validate .gobby/tmp/task-21558/native-ws-transport.md -p /Users/josh/.gobby/worktrees/gobby/lane-3-runbooks`; exit 0, one phase. The final frozen bytes are revalidated before sending. No implementation checks, daemon runs, pytest, cargo tests or activation proof have run. Expansion validation is intentionally unrun until Adv4 applies the server-derived M1 and W4 validates it. Josh's approval still precedes expansion. No deterministic result substitutes for Adv2's qualitative pre-audit.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Shared subscribe/unsubscribe/broadcast corpus
  category: test
  task_type: chore
  depends_on: []
  validation_criteria: 'A1.1: Every declared corpus case replays through the existing
    Python implementation with the documented single acknowledgement-set normalization.
    test: `tests/servers/test_ws_envelope_golden.py::test_python_replays_envelope_case`.

    A1.2: Both entry modes and absent/explicit-null/initialized transitions, including
    unsubscribe-before-subscribe, replay without modifying production behavior. file:
    `tests/fixtures/ws_envelope_golden/cases.json`.

    A1.3: Broadcast and fragment strings compare byte-identically, and direct responses
    are distinguished from broadcasts. test: `tests/servers/test_ws_envelope_golden.py::test_frame_bytes_and_direct_delivery`.

    A1.4: Version and case inventory drift fails rather than silently skipping data.
    test: `tests/servers/test_ws_envelope_golden.py::test_manifest_is_complete`.'
  labels:
  - covers:native-ws-transport:A1:A1.1
  - covers:native-ws-transport:A1:A1.2
  - covers:native-ws-transport:A1:A1.3
  - covers:native-ws-transport:A1:A1.4
  tdd: false
  source_section: A1
  assigned_agent: backend-developer
- title: Native subscription codec and corpus replay
  category: code
  task_type: feature
  depends_on:
  - A1
  validation_criteria: 'A2.1: Rust replays every A1 case with identical transition,
    recipient and output results. test: `crates/gdaemon/tests/ws_envelope_golden.rs::rust_replays_envelope_corpus`.

    A2.2: Both public entry modes, absent/explicit-null/initialized transitions and
    unsubscribe-before-subscribe retain their distinct outcomes. symbol: `SubscriptionState`
    in `crates/gdaemon/src/websocket/subscriptions.rs`.

    A2.3: Every non-string member/type class, mixed-member list and invalid JSON/object
    produces the specified bounded error without partial mutation; native-only negative
    cases are distinguished from supported-domain parity. test: `crates/gdaemon/tests/ws_envelope_golden.rs::malformed_control_does_not_mutate_state`.

    A2.4: Application frames are not reserialized or interpreted as subscription controls.
    test: `crates/gdaemon/tests/ws_envelope_golden.rs::application_frames_are_opaque`.'
  labels:
  - covers:native-ws-transport:A2:A2.1
  - covers:native-ws-transport:A2:A2.2
  - covers:native-ws-transport:A2:A2.3
  - covers:native-ws-transport:A2:A2.4
  tdd: true
  source_section: A2
  implementation_domain: backend
- title: Bounded native broadcast fanout
  category: code
  task_type: feature
  depends_on:
  - A2
  validation_criteria: 'A3.1: Only matching registered clients receive a batch; fragment
    bytes and per-client order remain exact. test: `crates/gdaemon/tests/ws_broker.rs::fanout_preserves_selection_and_frame_order`.

    A3.2: A subscription acknowledgement precedes publications selected by its new
    state. test: `crates/gdaemon/tests/ws_broker.rs::acknowledgement_precedes_newly_selected_events`.

    A3.3: A stalled/overflowing client cannot block healthy recipients or grow queues
    beyond both limits. test: `crates/gdaemon/tests/ws_broker.rs::slow_client_isolated_and_bounded`.

    A3.4: Simultaneous disconnect and failure invokes cleanup once, and each entry
    mode reconnects with its own original initial state. test: `crates/gdaemon/tests/ws_broker.rs::retirement_is_once_and_reconnect_is_fresh`.

    A3.5: Direct replies use the same ordered writer without subscription filtering.
    test: `crates/gdaemon/tests/ws_broker.rs::direct_reply_bypasses_event_filter`.'
  labels:
  - covers:native-ws-transport:A3:A3.1
  - covers:native-ws-transport:A3:A3.2
  - covers:native-ws-transport:A3:A3.3
  - covers:native-ws-transport:A3:A3.4
  - covers:native-ws-transport:A3:A3.5
  tdd: true
  source_section: A3
  implementation_domain: backend
```
