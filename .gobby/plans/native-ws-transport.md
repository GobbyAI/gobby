Plan artifact: `.gobby/plans/native-ws-transport.md`

# Native WS transport in gdaemon

**Plan ID:** native-ws-transport
**Root task:** #21558 — Native WS transport in gdaemon.
**Status:** canonical narrative; Adv2 pre-audit consensus 43c0f7fa; PD review, Josh approval and expansion pending.

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

Native gterm operation without a daemon is an invariant. Nothing in this plan changes gterm, gclient, JSON-lines control, bincode frames, or their independent connection paths. Future hub-to-node relays may consume the frozen envelope, but no relay, replay log, durable event bus, or node protocol is introduced here.

**Granularity:** three independently testable outcomes precede one gated integration outcome: corpus characterization, pure subscription codec, and bounded fanout. The cutover wires private application connections and publication together because switching only one loses or duplicates events. It does not absorb credential validation or application state machines.

## C2 Research and evidence rules
`kind: framing`

Source inspection uses the sole Lane 3 worktree. Draft branch `task-21558` is based on `lane-3-runbooks` at `766ba94a11b3a5c088668c88e01bb0b6dbc13c51`. Earlier observations at documentation-only HEAD `64f5e46e4cc3ed0138c837f9d77d1d595e8b3cac` have identical referenced source bytes. A read-only diff against `0.5.0` found no WS/server source differences; only `crates/gdaemon/tests/cli_contract.rs` differed in the inspected directories. Recheck the integration anchors after #23519 lands; the new auth surface is planned by that task, not present in this draft's source baseline.

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

## D1 Auth-integrated private bridge and native WS activation (depends: A1, A2, A3)
`kind: deferred`

```yaml
deferral:
  task_ref: "post-key-cutover-native-ws"
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

Targets for that gated implementation:
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`
- `crates/gdaemon/src/front_door/ws.rs::*` — scope-reason: replace application WS splice dispatch with native external transport while retaining development HMR splice
- `crates/gdaemon/src/serve.rs::*` — scope-reason: instantiate one shared native WS state and drain it on shutdown across both public listeners
- `crates/gdaemon/Cargo.toml`
- `crates/Cargo.lock`
- `crates/gdaemon/src/websocket/transport.rs`
- `crates/gdaemon/src/websocket/publish.rs`
- `crates/gdaemon/src/websocket/mod.rs::*` — scope-reason: register the gated runtime integration modules and constructor
- `src/gobby/servers/websocket/native_bridge.py`
- `src/gobby/servers/websocket/native_publish.py`
- `src/gobby/servers/websocket/server.py::WebSocketServer._handle_connection`
- `src/gobby/servers/websocket/server.py::WebSocketServer._handle_message`
- `src/gobby/servers/websocket/server.py::WebSocketServer.start`
- `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast`
- `src/gobby/servers/_app_ui.py::_mount_ws_endpoint`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/runner_front_door.py::*` — scope-reason: add the child-incarnation notification registration/state and bounded replacement notification to the existing monitor, with disarm cancellation; preserve its spawn/backoff policy
- `tests/test_runner_front_door.py::*` — scope-reason: extend the existing child/runner harness for notification, idle-respawn, failed-bind and shutdown races
- `tests/servers/test_native_ws_bridge.py`
- `tests/servers/test_native_ws_publish.py`
- `tests/e2e/test_native_ws_transport.py`
- `crates/gdaemon/tests/native_ws_transport.rs`
- `crates/gdaemon/tests/ws_golden_proxy.rs::corpus_replays_byte_equal_through_proxy`
- `docs/contracts/native-ws-transport.md`

**Granularity:** this is one gated activation outcome, not a monolithic replacement of application handlers. The pure codec/broker state machines were separated into A2/A3. Private bridge admission, native external dispatch and publication must activate together; otherwise one class of replies or broadcasts silently disappears. Move the additional runtime machinery into the listed new files. The current WS server is 805 lines, runner initialization 710, and Rust serve 390; leave existing production files below 850 and every hand-maintained production file below 1,000. Tests are exempt. Resweep exact symbols and consumers after #23519; an observed symbol rename is a fresh anchor correction, not permission to invent source.

Consumers unchanged (current baseline, to recheck when this tail is materialized):
- `src/gobby/servers/app_factory.py` — no-edit-reason: retain the mounted endpoint's public function signature and re-export while the front door takes external ownership.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: retain the init_servers re-export and signature.
- `tests/servers/test_app_factory_ui_modes.py` — no-edit-reason: the Python-only UI mounts remain available under the existing isolated mode.
- `tests/servers/test_ws_asgi_endpoint.py` — no-edit-reason: browser welcome/auth-close behavior is preserved; additional native/private-route coverage is owned by the new tests.
- `tests/test_runner_lifecycle.py` — no-edit-reason: the runner initialization/lifecycle contract stays intact.
- `tests/servers/test_websocket_server_disconnects.py` — no-edit-reason: existing connection cleanup behavior is preserved and extended by the new bridge tests.
- `tests/servers/websocket/test_broadcast.py` — no-edit-reason: existing producer signatures and Python characterization remain unchanged outside activated native mode.
- `tests/servers/test_websocket_subscriptions.py` — no-edit-reason: supported Python controls remain unchanged and supply A1's oracle.
- `tests/terminals/test_composer_proof_events.py` — no-edit-reason: existing supported subscription/event selectors retain parity.

These verified existing paths are baseline literal-sweep coverage, not a claim that overlay graph usages were exhaustive. Any consumer whose assumptions differ after #23519 must move into the integration Targets before its implementation plan is finalized.

**Research context:** E1/E2 place both public listeners in the current Rust front door and route every upgrade through splice; E19 proves its backend request and byte copy. E11 identifies the application dispatch table that stays in Python. E12 identifies Python's registration, welcome, pending interactions and disconnect cleanup; its welcome fields must survive exactly. E13 shows browser handshake-first admission with cookie authentication and close 4401; E14 shows the current direct bearer rejection shapes. E20 distinguishes browser subscription initialization; E22 preserves Authorization-before-cookie precedence and HTTP-only break-glass eligibility. #23519's authoritative task description supersedes the bearer verifier: `GOBBY_FRONT_DOOR_SECRET`, native operator key resolution, preserved managed/break-glass headers and Python front-door identity callback are planned there. The bridge uses that landed contract without recreating token-file helpers. The separate #23523 card confirms live activation is a needs-planning tail after the key-cutover leaves, not a consequence of #23519 closing. E6 proves why raw publication frames and direct replies need separate paths; E9 fixes the terminal corpus location. E10/E15 define existing transport limits/startup; E25 supplies the existing enabled switch and E26 anchors HTTP-before-subsystem binding. E27/E28 identify the sole child-respawn monitor and its disarm cancellation; E29 anchors runner wiring, E30 successful child startup, and E31 the one-time WS subsystem start. E16 supplies the existing WebSocket dependency and E18 the gclient entry path.

Implementation specification:

1. Native dispatch claims application upgrades on the configured public WS listener and the HTTP listener's `/ws` and `/ws/{path}` application paths. Preserve the direct listener's root entry and gclient's `/ws` entry (E18). Other public HTTP upgrades, including Vite HMR, retain splice behavior. Both application entry points share one broker, not one per listener. HTTP route dispatch and existing TLS/loopback policy remain intact.
2. Operator bearer/API keys pass exactly once through #23519's native front-door resolver. Managed `gobby-agent-v1` bearers remain for Python's existing managed-capability policy, not for an operator-key verifier. Strip all caller-supplied identity/bridge-control headers before creating trusted private context. Preserve the original public entry mode, scope type `websocket`, path/query, scheme and front-door-observed peer, plus Authorization presence/classification, original managed or otherwise unconsumed Authorization, original Cookie, and original break-glass header. Carry resolved operator user/machine/key IDs separately from caller credentials. Never replace the original WS scope/peer with the private bridge's loopback HTTP location. The pair secret authenticates this context; a client cannot select its entry mode, rewrite its public path or fabricate operator IDs.
3. Mount the backend-only application bridge at `/_gobby/ws/bridge` on the existing private Python HTTP/ASGI server, authenticated with the per-boot front-door secret. Both Rust public entry modes use this one backend HTTP address from existing backend-port resolution; do not select the direct listener's old Python WS target for the bridge. The dedicated Python WS listener supplies only the existing Python-only mode, not a second private admission path. Public paths with the reserved bridge prefix are refused rather than proxied. The bridge checks the secret in constant time, then applies the original entry's existing credential owner/precedence using the trusted original context. Direct entry uses #23519's front-door WS identity admission and requires resolved operator identity; cookies cannot turn direct-entry managed/rejected Authorization into operator admission. Browser entry uses AuthService's request admission on a reconstructed original `websocket` scope: verified operator identity first; otherwise original managed Authorization policy before cookie; cookie is checked only by this bridge. The current managed route matrix excludes `/ws`, so managed+cookie is rejected on both entry modes rather than falling through to cookie. Break-glass remains HTTP-only and cannot gain WS eligibility from the bridge's private transport. A valid cookie with an otherwise ineligible break-glass header admits only the browser cookie path; direct still refuses. Invalid/revoked operator keys remain front-door refusals even with a valid cookie. No raw operator key is checked again in Python, and no private metadata turns a rejected credential into a resolved identity.
4. Establish one bounded private connection per external client, with version-1 private JSON outer records: `channel: control` versus `channel: application`. Application records carry a text payload unchanged or a base64-encoded binary payload with an explicit encoding field; decoding restores exact original bytes. Admission has exactly one pre-application readiness phase. Only its first authenticated `control/bridge_ready` record supplies admission, settings and opaque transport ID; repeated/late readiness, unknown controls or forged context fail the bridge closed. A public application frame whose JSON contains `type: bridge_ready` stays inside an `application` record and is never consumed as private control. No private outer record reaches the public socket. The adapter feeds current Python handlers, preserves welcome/client/conversation IDs and pending interactions, and invokes their cleanup once. It receives the trusted entry mode for the subscription initialization in A2; Rust alone consumes subscribe/unsubscribe. Application text/binary payloads and close semantics remain opaque. Logical Python bridge clients bypass the old broadcast fanout, while their direct replies remain unfiltered. No application handler or broker registration runs before admission; browser accept-then-4401 and direct pre-upgrade HTTP refusals remain entry-specific. Bound encoded private records before allocation and decoded application bytes against the configured maximum; malformed base64, unknown encodings and unsupported private versions terminate the bridge.
5. Add the explicit internal publish channel at `/_gobby/ws/publish` on the front door. It admits only loopback paired-Python requests with the same per-boot secret; valid user bearer/cookie credentials alone are insufficient. Handle it separately from generic user authentication and refuse public forwarding. Rust creates a fresh random generation ID at each native transport boot, independent of the pair secret reused across child respawns. The endpoint has two private operations: `bind` installs the paired Python transport settings and returns that generation; `publish` accepts one batch containing a filter object and the already encoded frame strings from `BroadcastMixin.broadcast(message, frames=...)`, plus that bound generation. Bind has the same loopback/secret checks, a two-second deadline and bounded configuration body; it needs no external client. Public admission remains closed until bind succeeds. Bridge readiness must match the bound settings and generation, rather than supplying global configuration for the first time. Cap the total frame bytes at A3's 64 MiB bound and each public frame at the configured message-size bound. Account for JSON escaping separately with a bounded encoded HTTP body limit of 128 MiB. The receiver rejects wrong generations, wrong secrets, malformed/oversize batches and non-loopback callers. It enqueues once into A3 and never reserializes the public frames.
6. The Python publisher uses an existing asynchronous HTTP client with a two-second deadline and bounded request size; no durable queue and no retry after an ambiguous response. Forward publication even when Python has no direct public clients. Reuse the existing `front_door.enabled` choice: Python-only mode keeps its current fanout; an enabled native front door has exactly one native fanout owner and never broadcasts through both. Native readiness failure refuses admission and never falls back to an application splice or Python fanout. No new proxy/native/compare toggle is introduced. On a failed/ambiguous publish, atomically mark that generation failed and stop publication to it. The reachable actuator is the local registry of that generation's paired application bridge legs: close them concurrently within a one-second bound, terminating their adapter handlers even when the publish HTTP channel is blackholed. Rust's independent private-leg reader treats closure/EOF as retirement and closes its public connection within one further second through A3. No invalidation HTTP request or functioning publish response is needed. Cleanup remains once-only; an old generation cannot close a new generation. Test the full two-second publish plus two-second retirement bound. Clients reconnect/reconcile through existing paths; never claim delivery or replay an ambiguous batch.
7. Rust uses the existing workspace WebSocket dependency/version family (gclient already uses tokio-tungstenite 0.26), rather than hand-writing RFC frame parsing. The new module negotiates only implemented extensions, supports masked text/binary/continuation frames, ping/pong and close, and preserves current max-message size and ping settings supplied by authenticated publisher bind and checked by bridge readiness. Until the bridge configuration is ready, admission fails closed. A missing backend is a bounded unavailable/1013 outcome; never transparently reconnect an application bridge and replay its side effects. Cancellation closes the private and public legs and releases broker/handler state.
8. Wire the publisher and native bridge in the existing runner initialization and WS server. Perform the initial private bind from `WebSocketServer.start`, after the existing lifecycle has bound Python HTTP (E26), never synchronously during constructor initialization or front-door child startup. Bind succeeds before the WS subsystem reports ready; failure reports a bounded subsystem failure and keeps native admission closed. In `run_gobby`, immediately after assigning `active.front_door_child` (E29) and before `active.run`, register the publisher with the existing child monitor. Registration exposes the current successful child incarnation; a monotonically increasing local incarnation counter identifies each successful `FrontDoorChild.start`, independently of PID reuse. The initial publisher start binds that current incarnation. After each successful replacement `start` returns in `_watch` (E27/E30), the monitor invokes the registered async publisher notification without requiring clients or publications. There is no second polling loop. Before publisher startup, retain only the latest incarnation; after startup, serialize notifications with initial bind and attempt exactly one authenticated two-second bind for each current incarnation. Retire the old publication state/bridge legs before installing the replacement. Commit a bind result only if its incarnation is still current and shutdown has not begun; late old results cannot overwrite or close new state. Duplicate notifications are idempotent. A replacement during initial or pending bind supersedes that attempt and triggers its own bounded bind. The replacement remains unavailable until this path succeeds. Bind failure is surfaced through the existing subsystem failure/logging path, keeps that incarnation unavailable, and never escapes to kill the respawn monitor or restarts a healthy child. No application batch is retried. `disarm` unregisters notifications and cancels pending binds on their owning loop; publisher shutdown awaits cancellation before retiring state. A failed generation cannot be reused or receive replayed publication; rebinding a distinct generation does not retry any failed application batch. Preserve current producer entry points, attention epoch/sequence values and caller-owned ordering locks. Shutdown stops admission, retires the broker and drains/closes bounded writers before listener teardown; it never kills gterm or its host. Remove the production application splice path after parity, while retaining the explicit HMR proxy. Any mode switch is the Orchestrator's announced cutover using the repository binary-set promotion contract; this planning seat does not run it.

Obligations retained for the deferred task:

- D1.1 - Both application entry points use native Rust upgrade/lifecycle and one broker; HMR remains proxied. test: `crates/gdaemon/tests/native_ws_transport.rs::application_paths_are_native_and_hmr_is_proxy`.
- D1.2 - Operator key verification has only the #23519 owner; managed admission/cookie policy retains original WS context. Both entry modes cover managed+valid cookie (deny), operator identity+cookie (operator admission), break-glass+cookie (browser cookie admission only), rejected key+cookie (deny), cookie-only entry differences, forged peer/path/mode/identity and missing/stale pair proof. test: `tests/e2e/test_native_ws_transport.py::test_credential_ownership_and_private_channel_refusal`.
- D1.3 - Welcome, direct replies, text/binary bytes and once-only cleanup remain exact. Private readiness is one-time and phase-bound; repeated/late/forged readiness fails closed while a public application payload with the same type passes through and no private controls leak. test: `tests/servers/test_native_ws_bridge.py::test_bridge_preserves_application_connection_lifecycle`.
- D1.4 - Native fanout occurs once, direct replies bypass filtering and ambiguous publication never retries. A blackholed HTTP publish channel retires only the affected generation through local bridge-leg closure within the stated bound, including an old/new generation race. Initial binding without any clients succeeds after HTTP startup; failed binding keeps admission closed, and child respawn requires a distinct authenticated generation. An isolated idle-child crash with no clients or publications must drive the real monitor notification and restore admission without a manually invoked bind. Failed rebind, crash during initial bind, duplicate/stale notification and shutdown races must remain closed or preserve only the current generation. test: `tests/servers/test_native_ws_publish.py::test_single_fanout_owner_and_ambiguous_publish`.
- D1.5 - Both runtimes replay the shared event corpus and the existing terminal corpus remains byte-identical through native transport. test: `crates/gdaemon/tests/native_ws_transport.rs::terminal_and_envelope_corpora_replay_native`.
- D1.6 - Message limits, ping/close, backpressure, backend failure, reconnect and shutdown are bounded and leave no client/handler/lease residue. test: `tests/e2e/test_native_ws_transport.py::test_failure_reconnect_and_shutdown_are_bounded`.
- D1.7 - Native gterm direct access and a running terminal survive daemon loss/restart independently of the new WS listener. test: `tests/e2e/test_native_ws_transport.py::test_gterm_remains_available_without_daemon`.
- D1.8 - The public/internal frame boundary, credential owners, limits, failure semantics and cutover proof are documented; no retired verifier or application proxy fallback remains active. file: `docs/contracts/native-ws-transport.md`.

**Focused verification (planned):** isolated tests with temporary homes, synthetic secrets, test-hub database and ephemeral ports. Run the new bridge/publish tests, current subscription and terminal-golden Python harnesses, the new native transport e2e file, Rust native transport and shared-corpus tests. Run `tests/test_runner_front_door.py` for real monitor notification, idle respawn, failed rebind and disarm coverage. Run `tests/servers/test_ws_asgi_endpoint.py` and `tests/servers/test_app_factory_vite_proxy.py` for existing entry/HMR behavior, and the composer-proof tests for subscription consumers. Sweep every changed production/test consumer before finalizing targets. No full pytest run. Integration tests must not open the operator's token/bootstrap files or contact the running daemon. Implementation requires TDD RED/GREEN/final GREEN, scoped lint/type/test audits and source-based acceptance review.

## V1 Plan Changelog
`kind: framing`

2026-10-06: Initial narrative draft by gobby#15382 on LM7's authorized writer loan. Transport-only ownership and #23519 auth boundaries follow the Orchestrator's 18:42 CT ruling. Adv2 pre-audit and consensus are pending. No M1 has been authored.

2026-10-06: Adv2 audit `0c6cbba0-4090-4506-9a20-73f423365787` verified all initial source quotes and found four blockers. Entry-mode initialization and absent/null transitions are now explicit; malformed native members/types are validated atomically outside the unchanged Python parity domain; bridge admission carries original credential/scope context with a two-entry test matrix; and D1 is externally gated by both #23519 source and #23523 live activation. Readiness framing, a local bridge-leg retirement actuator, and baseline unchanged consumers address the advisory details. Re-audit and consensus remain pending; no M1 or main write.

2026-10-06: Adv2 re-audit `96eb07e9-d046-4163-9297-6887cd630263` cleared WS-A2-01 through WS-A2-04 and identified WS-A2-05: no production trigger for idle child rebind. The existing respawn monitor now owns a bounded async replacement notification, registered by run_gobby to the publisher with current-incarnation ordering, startup/shutdown race handling and surfaced failed-bind behavior. Integration targets and acceptance coverage include the real idle-respawn path. Re-audit/consensus remain pending.

2026-10-06: Pre-audit consensus reached with Adv2 gobby#15414, receipt `43c0f7fa-a836-4109-8ca5-b34fc4391e07`, on behavior, decisions, all 13 active criteria and eight deferred obligations. WS-A2-01 through WS-A2-05 are resolved; Adv2 independently verified all 84 quoted source lines (per Adv2’s packet correction) and base validation passed. WS-A2-06 inventory nit is corrected with an explicit whole-file scope reason for the existing runner/child test harness. Final-byte check remains pending. M1, canonical GO, Program Director review, Josh approval and expansion remain separate gates; none is implied by pre-audit consensus.

## E1 Source evidence
`kind: framing`

The following are hash-verified `gcode evidence` reads, complete with no warnings. Every quote uses the returned numbered excerpt. They describe the current baseline, not #23519's future implementation.

- E1 — `crates/gdaemon/src/serve.rs::run`, excerpt `b20471c8b32029dda3aec791fa10fb87f8e664e9069e105027fba9155a080b32`: `72| let mut listeners = bind(host, bootstrap.daemon_port, backend_http).await?;` and `73| listeners.extend(bind(host, bootstrap.websocket_port, backend_ws).await?);`.
- E2 — `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle`, excerpt `29c34eadc4bb20cf4a0547f45e459924143b0cb24965e6c7077a6be194f48768`: `63| if ws::is_upgrade(request.headers()) {` and `64| ws::splice(&self.state, request).await`.
- E3 — `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._handle_subscribe`, excerpt `9de81f0f8df694bb3b904aa80caa5dd153c0b6dfc5269f5e87303430f94ec3bd`: `189| websocket.subscriptions.update(events)` and `196| "events": list(websocket.subscriptions),`.
- E4 — `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._handle_unsubscribe`, excerpt `a4aed7ed4bac277dbe6f28071917e5fa68d3926cd8054046bf560bf16acca369`: `217| if not events or "*" in events:` and `218| current_subscriptions.clear()`.
- E5 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin._is_subscribed`, excerpt `ec3ddde0936b6a6afcb72c47909ee6129f6f4ee90af8ff25c1bfb39360b29eb7`: `83| if subs is None:` / `84| return False`; `116| if msg_type not in event_types:` / `117| return True`; `133| if message.get(key) == value:` / `134| return True`.
- E6 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin.broadcast`, excerpt `72f3b2ac1de123c1b2b14b9c645ef7be72270320c99cea28fba82f2301b55902`: `193| payloads = [json_dumps(frame) for frame in frames] or [json_dumps(message)]`, `201| if self._is_subscribed(websocket, message):`, and `209| *(self._send_payloads(websocket, payloads) for websocket in recipients)`.
- E7 — `src/gobby/servers/websocket/broadcast.py::BroadcastMixin._send_broadcast`, excerpt `916d100eb1729547a3cb7cec1f586d491c2114ecdfc30263e4d6d9c0676543b5`: `149| timeout=BROADCAST_SEND_TIMEOUT_SECONDS,`, `159| websocket.close(code=1011, reason="Broadcast send timed out"),`, `160| timeout=BROADCAST_CLOSE_TIMEOUT_SECONDS,`.
- E8 — `src/gobby/servers/websocket/broadcast.py`, excerpt `9869eea5ea881d51d6923b803502593d2133e125b01cfd943e70a913495d8f0f`: `35| BROADCAST_SEND_TIMEOUT_SECONDS = 2.0` and `36| BROADCAST_CLOSE_TIMEOUT_SECONDS = 1.0`.
- E9 — `crates/gdaemon/tests/ws_golden_proxy.rs::corpus`, excerpt `0f0b7e41a02c5662a2456148fcfe324a0f908242f1f920684388baa50e30c00c`: `14| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/terminal_ws_golden");`, `16| &std::fs::read(dir.join("manifest.json")).expect("read golden manifest"),`, and `25| let bytes = std::fs::read(dir.join(&name)).expect("read fixture");`.
- E10 — `src/gobby/servers/websocket/models.py::WebSocketConfig`, excerpt `60182f482fe10776d2cb8a8a708e33bc66a268b1ef1bda6604b3c4f2f493c8a5`: `40| ping_interval: int = 30  # seconds`, `41| ping_timeout: int = 10  # seconds`, and `42| max_message_size: int = 5 * 1024 * 1024  # 5MB (increased for voice audio)`.
- E11 — `src/gobby/servers/websocket/server.py`, excerpt `4f1607e4d7d4feca7d142e21cdd46a6362ffd05a0f119f4b789fd9183c1f3c3e`: `522| "subscribe": self._handle_subscribe,`, `523| "unsubscribe": self._handle_unsubscribe,`, `525| "terminal_input": TerminalWsMixin._handle_terminal_input.__get__(self),`, and `526| "chat_message": self._handle_chat_message,`.
- E15 — `src/gobby/servers/websocket/server.py::WebSocketServer.start`, excerpt `c6893053d5da62735866bdb7d8e6bd5f772bc416ee91d6b1e2411a312e11e79f`: `694| process_request=self._authenticate,`, `695| ping_interval=self.config.ping_interval,`, `696| ping_timeout=self.config.ping_timeout,`, `697| max_size=self.config.max_message_size,`, and `698| compression="deflate",`.
- E12 — `src/gobby/servers/websocket/server.py::WebSocketServer._handle_connection`, excerpt `af9ab343adac977985d1a029fa27bc707d03595a7c141eda0e4fffd7cdac1592`: `418| client_id = str(uuid4())`, `442| "type": "connection_established",`, `479| await self._cancel_off_loop(websocket)`, `480| await self._cleanup_terminal_client(websocket)`.
- E13 — `src/gobby/servers/_app_ui.py::_mount_ws_endpoint`, excerpt `c2279c93d1ad91b5cbb4a55830a0b36094770054bb703275bf4ec7bd1664db5d`: `55| await adapter.accept()`, `62| server.auth_service.is_request_authenticated,`, `66| await adapter.close(code=4401, reason="Authentication required")`.
- E14 — `src/gobby/servers/websocket/auth.py::AuthMixin._authenticate`, excerpt `59a7fadcd68aa62a657ccd33bca6294c79651b23c7945b36867b1b727ac5cb3d`: `39| auth_header = request.headers.get("Authorization")`, `68| user_id = await self.auth_callback(token)`, `75| 403,`. This is the pre-#23519 path to replace, not a verifier to retain.
- E16 — `crates/gclient/Cargo.toml`, excerpt `faefdae5b7686605cd382706653060ef291a1a71b3c60033418258d32944255d`: `38| tokio-tungstenite = { version = "0.26", default-features = false, features = ["connect", "rustls-tls-webpki-roots"] }`.
- E17 — `crates/gclient/src/daemon/live_reader.rs`, excerpt `819b3a4734ae3971d95781ddff434bf912b8f32566c42382354517530cd841b8`: `21| const MAX_ASSEMBLY_BYTES: usize = 16 * 1024 * 1024;` and `22| const MAX_AGGREGATE_BYTES: usize = 64 * 1024 * 1024;`.
- E18 — `crates/gclient/src/daemon/live_reader.rs`, excerpt `6a3a67c5aa177215fb83cbb28d49dca79b662cbee2219d624fa44f4f1f3697d0`: `98| url.set_path("/ws");` and `103| request.headers_mut().insert(AUTHORIZATION, authorization);`.
- E19 — `crates/gdaemon/src/front_door/ws.rs::splice`, excerpt `5696605731859336dd111e4a001921773c84e0e203cda5327bedb4a5d3d141ce`: `46| let mut response = match sender.send_request(Request::from_parts(parts, body)).await {` and `63| let _ = tokio::io::copy_bidirectional(&mut client, &mut backend).await;`.
- E20 — `src/gobby/servers/websocket/asgi_adapter.py::ASGIWebSocketAdapter.__init__`, excerpt `1c7e663e90d6e0671f7bba9272ca99490efc2eb5cd418b58190e26afbacc6774`: `20| self.subscriptions: set[str] = set()`.
- E21 — `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._send_error`, excerpt `2752405e9ed79f3edd04c14fa253b9d2459a48785f2266cc78465160b7cf268d`: `39| code: str = "ERROR",`, `51| "type": "error",`, `52| "code": code,`, `53| "message": message,`.
- E22 — `src/gobby/servers/auth_service.py`, excerpt `5d7b8400da19c7dcde3c74b8f1f116664a44087b5e4458a7601a0b11ceab5262`: `352| authorization = request.headers.get("Authorization")`, `360| claims = self._classify_agent_token(request, parts[1])`, `361| return None if isinstance(claims, AgentApiTokenClaims) else claims`, `367| session_token = request.cookies.get(_SESSION_COOKIE)`, and `402| if request.scope.get("type") != "http" or request.client is None:` / `403| return False`.
- E23 — `src/gobby/servers/auth_service.py` complete capability matrix, excerpt `50b6c4be352a350556130bc112695a7f54d0ea714087ba998f14a628680ccd0e`: `91| _AGENT_CAPABILITY_MATRIX: tuple[_AgentRoute, ...] = (` through `132| )` lists API routes only, with no WS route. E24 supplies the fail-closed result.
- E24 — `src/gobby/servers/auth_service.py::AuthService._classify_agent_token`, excerpt `269255ec18097be3f9c330a6267befbce44fc98ae6ad3c79505777e7cb355f4a`: `449| entry = _agent_capability_allows(request)`, `450| if entry is None:`, and `451| return "route_not_permitted"`.

- E25 — `src/gobby/config/bootstrap.py::FrontDoorConfig`, excerpt `029b268290c859a065b7aca16a8620a31b4746c7bf9f49b5d033feb5ef8cff41`: `91| enabled: bool = True` anchors the existing mode choice.
- E26 — `src/gobby/runner_lifecycle.py::run_daemon`, excerpt `8cb1c873b635f2cee022ba6820b39673ad8bd4493657380b096ae2ef55f795ea`: `272| while not server.started and not server_task.done() and not runner._shutdown_requested:` waits for HTTP; `275| if server.started and not runner._shutdown_requested:` gates `279| runner._subsystem_init_task = asyncio.create_task(`. No lifecycle reordering is required.

- E27 — `src/gobby/runner_front_door.py::FrontDoorChild._watch`, excerpt `8daae9e3d34f6e42cec8db523db5a3ee74531d43eda4fee46f2e403461ed5075`: `278| await asyncio.to_thread(self.start)` replaces only the Rust child; `284| serving_since = time.monotonic()` marks successful replacement. This existing monitor owns the new async publisher notification.
- E28 — `src/gobby/runner_front_door.py::FrontDoorChild.disarm`, excerpt `fc0b54492a7361b43eb1588a1cd87adc0132e55de04e2aca8985a85c9695d7c9`: `152| self._disarmed = True` and `155| monitor.get_loop().call_soon_threadsafe(monitor.cancel)` anchor owning-loop shutdown cancellation.
- E29 — `src/gobby/runner.py::run_gobby`, excerpt `6020a3857c80fecdc2e7be913871b7db19e84936af442d72aebedf96d3adbd34`: `496| runner = await GobbyRunner.create(config_path=config_path, verbose=verbose)` and `499| active.front_door_child = front_door` anchor publisher/child wiring before run.
- E30 — `src/gobby/runner_front_door.py::FrontDoorChild.start`, excerpt `497240c944d72b830022f5674d456694379f72301411435261858cf7408c2615`: `131| self._spawn()` and `133| self._wait_until_serving()` establish a successfully serving child incarnation.
- E31 — `src/gobby/runner_lifecycle_subsystems.py::_start_websocket_server`, excerpt `0d1eee314acd88563d5b43451fc1f7b62b7dad6af97b3ab0fe8b6b1f23af3c0e`: `590| runner._websocket_task = asyncio.create_task(` and `591| runner.websocket_server.start(), name="websocket-server"` anchor one initial WS startup, not a child-respawn trigger.

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
