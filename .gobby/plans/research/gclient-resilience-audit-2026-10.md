## Research Findings

### Research Questions

- Which open leaves under #23220 gclient daemon resilience are already implemented on 0.5.0, partly implemented, or still needed?
- Which closes and exact remaining-scope criteria are supported by current source and Git history?

### Domain Context

- Audited 2026-10-23 against 0.5.0 commit `9930d2184a8d15d68bd3c561ef37863129667704`, tree `92018f06a4bf4aec22fddd5568a6c67d1852b4e8`. Every requested leaf was read using `gobby-tasks:get_task(brief=false)`; oversized records were fully retrieved with `gobby-results:get_tool_result`. All 17 were open with no linked commit in their records.
- Integration advanced during the audit to `79d94c0bd2c0e35585c14b4c91620c825ad5bef6` (#24850 web terminal pane focus). A final Git diff from the evidence binding through that tip showed no changes in the audited gclient/gterminal, terminal Python, pool, terminal handler, or contract/guide paths; findings still apply at that tip.
- Read-only source audit. No tasks, source files, installed binaries, processes or other sessions' resources were changed. This note is the sole requested research artifact. Audited source/docs paths had no uncommitted changes against HEAD. Other unrelated working-tree changes were left untouched.
- Josh's recorded boundary remains: daemon enhances native terminals; native launch, attach, render and typing must work degraded without it. Do not add watchdogs or route native keystrokes through the daemon. Memory search returned b59e4ce9-396a-5119-98e6-c14ad4c12d2b and current outage guidance 392cc53f-01d7-532f-a037-49c29891b561. Source, rather than memory, decides completion.
- Current #24815 outage support asks the owner-only host control socket for a grant when an existing direct pane needs one. That differs from the plan's frame-connection-owned fallback holder. It implements useful outage behavior but does not by itself satisfy native inventory, first-local-holder arbitration, unconfirmed leases, or all-pane reconfirmation.
- Verdict: **0 done; 6 partly done; 11 still needed. No whole-leaf close with `already_implemented` or `obsolete` is supported.** Existing scaffolding or a similarly named test is insufficient to claim the complete acceptance contract.

### Evidence & Sources

**Summary and disposition**

| Leaf | Current classification | Criteria assessment / proposal | Evidence |
| --- | --- | --- | --- |
| #23230 Open unresolved terminals as jobs | still needed | A3b.1/.4 absent; A3b.2 helper exists, but live Workspace events still await it. Keep criteria. | E1,E2 |
| #23231 Event-driven refetches and roster jobs | still needed | A4a.1–.3 remain: async event apply awaits roster/attention; existing relist/sidebar jobs do not supply scoped Roster/Attention outcomes. | E1,E3,E4 |
| #23232 Lifecycle and shell-adoption actions as chained jobs | partly done | A4b.2 kill-before-close behavior exists. Create/adoption/arrange still await; committed-orphan ownership and A4b.1/.3–.9 remain. | E5,E6,E7 |
| #23233 Orphan inventory and destroy fan-out as jobs | still needed | A4c.1 absent: orphan fetch awaits; kill loop is serial and aborts on transport error. | E8 |
| #23234 Launch and reconnect audit and WARN bracket | partly done | Staged startup/reconnect and frame-recovery jobs exist. A5.2 stale reconcile proof, A5.4 complete path audit, A5.5 WARN bracket and A5.6 held-REST chord proof remain. | E1,E3,E9 |
| #23235 Daemon health from in-flight age | still needed | B1.1–.4 absent: raw reply senders have no issued_at; no Slow state/status implementation found. | E10 |
| #23236 Shed load while slow | still needed | B3.1/.2 absent: periodic requests depend on elapsed time/startup state, with no Slow gate. | E11 |
| #23237 Native inventory and fallback input authority in gterm | still needed | C0.1–.7 incomplete: existing token/grant/refusal primitives exist; no frame inventory variants, per-stream fallback authority or grant-connection fence. | E12,E13 |
| #23238 Read native inventory through the existing frame socket | still needed | C0b.1–.3 absent: no NativeHost inventory consumer; existing direct attach is not daemonless inventory. | E12,E14 |
| #23239 Launch, render and type native panes with no daemon | still needed | C0c.1–.8 incomplete: valid-token startup can enter an unconnected loop; missing default credentials still error and no native inventory/projection/adoption path exists. | E9,E14,E15 |
| #23240 Keep control on disconnect for direct-granted panes | partly done | C1.1 typing and C1.2 proxy clearing have shipped behavior, but no lease_unconfirmed state or C1.3/.4 contract/status text. | E16,E17,E18 |
| #23241 Reconfirm the lease on reconnect | partly done | Host-grant rejection and fresh host registration exist; reregister retakes focused pane only. C2.1–.5 all-pane confirmation, stream continuity, bounded retries and unconfirmed state remain. | E19,E20 |
| #23242 Direct re-attach before proxy fallback | partly done | Direct-first local host recovery exists, but restore asserts host control immediately; C3.1 unconfirmed state, C3.3/.4 fallback races and C3.5 all-pane retakes remain. | E20,E21 |
| #23244 Bounded HostClient round trips | still needed | D3.1–.10 incomplete: generic roundtrip/locks/cleanup unbounded; aggregate deadlines, written-request retirement/no-replay and release evidence remain. | E22,E23 |
| #23245 Host grant reconciliation never holds the terminal lease lock | partly done | Narrow daemon_shutdown preservation exists. D3b.1–.6 remain: ws_close preserves nothing, host observer still awaited under lease lock, no separate sync/cancellation convergence. | E24,E25,E26 |
| #23247 Drop the undrained host event subscription | still needed | E1.1 bug persists in relocated native_frames.py; E1.2 forced helper extraction is stale scope rather than the leak fix. | E27,E28 |
| #24103 Recover #23246 pool exhaustion census: rate-limited nonblocking pg_stat_activity census on final pool acquire failure | still needed | Criteria 1–5 remain: final retry failure logs pool_stats only; census commit never reached audited integration. | E29 |

**Commit attribution**

These commits explain implemented subsets; none is a completing commit for an audited whole leaf.

- `528cf50cc0` ([gobby-#23228] typed input and control path off the loop): existing workspace-op/kill jobs and host-grant response handling. #23232 must reuse this close/kill behavior rather than rebuild it. E6, E19.
- `2645d25f10` and subsequent `fbd96ab00d` ([gobby-#23229]): attach/recovery jobs and stale/retired recovery-result settlement. This is partial groundwork for #23234; a settled frame-recovery result does not prove a stale full reconcile is dropped. E9, E21.
- `926f902717` ([gobby-#23076]): host-local direct reconnect before daemon fallback. Current code has since been decomposed; E20/E21 are current locations. Later #23419 commits `6a5047cfd9`, `31802b1421` and `613b309f8d` handle daemon generation changes and queued takeover, but current reregister still retakes only the focused pane.
- `e5052c357f`, `649064e366`, `8cf34fbc78`, `34e3c3bd0e` ([gobby-#24815]): direct outage typing/scrolling, per-write failure reporting, contract update and frame_source control dial. E16/E17 and `crates/gclient/tests/outage_direct_input.rs`.
- `23328c7392` ([gobby-#23519]) is the latest change to the moved native frame helper, but the subscription at E27 remains. File movement is not E1 completion.
- `269596745e` ([gobby-#23299]): sanitized `psycopg_pool.*` logging **without the census**. `2c88be74b6f6ad12f531bf93480000f61ca06725` ([gobby-#23246]) exists in Git but is not an ancestor of audited HEAD; current E29 confirms the census remains missing. #24103 correctly owns recovery.

**Exact proposed rescopes**

Orchestrator-only proposals; no mutations applied. For leaves not listed below, retain the recorded acceptance criteria verbatim, reproduced later in this note. Retain applicable validation, TDD and release/cutover gates; this audit supplies no retrospective red/green evidence.

- **#23232 Lifecycle and shell-adoption actions as chained jobs**
  1. Retain A4b.1 and A4b.3–A4b.9 verbatim. Convert remaining create, adoption, location, arrange and grid waits through existing jobs; preserve returned-id chaining, rollback, and stale committed-result disposal in both completion orders.
  2. Replace A4b.2 with: “Preserve the existing `close_live_tab` / `kill_live_panes` job chain. With each owned kill held in turn, frames, ticks and unaffected direct input advance; no `TabClose` is issued before all kills settle. Any refused or failed kill retains its pane/tab. A successful all-killed chain issues at most one `TabClose`. Add or adapt a focused behavioral test; do not introduce a second close ledger solely to rename shipped behavior.”
  3. Add: “Committed-orphan disposal is owned until success or not-found; transport loss/refusal retains the identity and one visible error until a later authoritative model install reevaluates it. Never compensate an unanswered create whose committed identity is unknown.”
  Existing A4b.6–.8 already require item 3; keep it explicit when narrowing implementation around shipped jobs.

- **#23234 Launch and reconnect audit and WARN bracket**
  1. Retain A5.2 and A5.4–A5.6 verbatim.
  2. Replace any implementation instruction to move launch/reconcile off-loop with: “Reuse the existing selected `startup_job`, `reconnect_job`, relist and recovery futures. A late reconcile snapshot/outcome from an older daemon generation cannot mutate current workspace, panes, control or receiver ownership; explicitly test the generation change while attach/roster is held.”
  3. “Provide a bounded source-to-test audit covering each daemon-touching select branch and post-select helper listed in A5.4, including inline event refreshes, lifecycle/adoption/arrange, theme sync and sidebar work in the current loop. A held request on each path advances frames/ticks and unrelated direct input. Each reconnect/reconcile attempt emits exactly one start and one terminal finish WARN with reason, generation, outcome and elapsed_ms.”
  Keep this leaf as a final audit of the completed path conversions; current staged startup alone is insufficient.

- **#23240 Keep control on disconnect for direct-granted panes**
  1. Replace C1.1 with: “Preserve writable direct-native sources and their existing host grant on daemon disconnect; mark their daemon lease unconfirmed without interrupting direct Input/Paste. A typed host refusal removes typing authority. Cover both an existing daemon grant and an outage-acquired host grant.”
  2. Retain C1.2: proxied panes enter Observe on disconnect.
  3. Replace C1.3/C1.4 with: “Document that existing native typing continues on the host while the daemon lease is unconfirmed, identify which enhancements are unavailable, and render/document a matching unconfirmed-lease status. Keep #24815's owner-only-control grant failure and proxied-input status behavior.”
  4. “Reuse `host_upgrade_recovery.rs::held_native_input_survives_daemon_outage_and_host_refusal_still_applies` and the outage-input suite where their assertions prove the behavior; add the missing unconfirmed-state assertions rather than require duplicate test names.”
  #24815 satisfies the preservation/typing subset. It does not provide the new state or full docs/status acceptance.

- **#23241 Reconfirm the lease on reconnect**
  1. Retain C2.1–C2.5's behavioral requirements: every unconfirmed direct pane, including unfocused and daemonless-adopted panes, rebinds its preserved source before its own take; typing continues until authority is denied; no duplicate source appears.
  2. “On host-confirmed grant clear unconfirmed state; on held reply enter Observe with take-back. A healthy-socket take failure retries at most three times, then enters Observe; successful peer panes confirm independently.”
  3. “A lease grant with host_input_granted=false never claims typing authority and remains unconfirmed until a later take confirms host input. Preserve the existing false-host-grant input gate.”
  4. “Extend existing host-reregister application beyond its current focused-pane conditional, and prove three panes independently complete, refuse or exhaust retries. Reuse #24815 outage-grant adoption where applicable.”
  Keep original named cases or equivalent tests with explicit coverage mapping. Existing manual take-back safety is partial C2.5, not bounded reconfirmation.

- **#23242 Direct re-attach before proxy fallback**
  1. Replace the already-shipped direct-first implementation instruction with: “Preserve existing host-local reconnect before daemon fallback, its finite budget, epoch/terminal identity checks, cancellation disposal and generation-aware reregister.”
  2. Retain C3.1: successful direct reattach marks lease_unconfirmed and displays `Reconnected; input authority unconfirmed.`; it does not assert daemon confirmation merely because the source was restored.
  3. Retain C3.2–C3.5 verbatim: document direct-first; prove local-holder reclaim against peer ownership, the daemon-return/stream-loss race, and fresh all-pane retake after reregister.
  4. “Reuse existing host_upgrade_recovery tests for direct-first success, refusal, timeout, cancellation and daemon generation changes; add the missing authority-state and multi-pane cases.”
  Current immediate `restore_host_control()` plus focused-only retake leaves the new authority contract incomplete.

- **#23245 Host grant reconciliation never holds the terminal lease lock**
  1. Retain D3b.1–D3b.6 verbatim, including superseded reply snapshots, latest-generation host effects, and cancellation recovery that snapshots after acquiring the separate holder-sync lock.
  2. Add: “Update both ordinary ws_close and daemon shutdown cleanup paths. For the qualifying current direct-native holder, ordinary ws_close must preserve the host grant as specified; explicit release/detach/removal/non-direct cleanup must still revoke or replace it. Preserve current durable daemon_shutdown handoff semantics unless deliberately replaced by an equivalent tested contract.”
  3. “The real HostClient unanswered-grant test observes a registered pending request while same-terminal lease-state acquisition and another connection lane complete; eventual deadline clears pending state. No host await remains under the lease-state lock.”
  Existing daemon_shutdown preservation is narrower than D3b's ws_close policy; it cannot justify closing the leaf.

- **#23247 Drop the undrained host event subscription**
  1. Replace E1.1 target with: “`NativeFrameStreamMixin.reserve_observer` in `src/gobby/terminals/native_frames.py` sends no `subscribe_events` on the main control client. Add/retain `test_reserve_observer_sends_no_subscribe_events` or an explicitly mapped equivalent.”
  2. “Remove the unused `_subscribed` field and its fake-runtime mirrors. Keep the separately opened `host_event_reader.connect_event_stream` as the event consumer; repeated observer reservation adds no event subscription on the main client.”
  3. Replace E1.2 with: “Keep touched production modules below the repository monolith ceiling. Extract spawn-failure helpers only if the actual edited module requires decomposition; do not require a new `native_spawn_failure.py` solely because the old plan observed 854 lines.”
  4. “Run focused protected native-runtime/runtime-contract/acceptance/native-spawn tests on the isolated test hub and the relevant source checks.”
  The forced extraction subcriterion is obsolete as a size-based prerequisite: current `native_runtime.py` ends at line 823, and observer behavior already moved out. The whole leak-fix leaf remains needed.

**Criterion-by-criterion ledger**

“Remaining” means the criterion cannot be marked complete from the inspected source. Existing support is called out even where a criterion still needs an integrated test.

- #23230: A3b.1 remaining; A3b.2 helper present, nonawaiting event half remaining; A3b.4 remaining.
- #23231: A4a.1 remaining; A4a.2 remaining; A4a.3 remaining. Existing sidebar/relist futures are reusable groundwork.
- #23232: A4b.1 remaining; A4b.2 source behavior implemented (E6), named proof absent; A4b.3/.4/.5/.6/.7/.8/.9 remaining. E7 explicitly defers arrange conversion to this task.
- #23233: A4c.1 remaining, including inventory liveness, concurrency and one result per requested target after failures.
- #23234: A5.2 remaining for reconcile (stale frame recovery is already handled separately); A5.4 remaining because blocking paths remain; A5.5 remaining (existing frame-recovery start/complete logs are a different bracket); A5.6 exact held-REST proof absent.
- #23235: B1.1/.2/.3/.4 remaining.
- #23236: B3.1/.2 remaining.
- #23237: C0.1/.2/.3/.4/.5/.6/.7 remaining as specified. Authentication, input refusal and persistent daemon input_grant are existing primitives, not frame inventory/fallback ownership or cross-control-connection fencing.
- #23238: C0b.1/.2/.3 remaining.
- #23239: C0c.1 partial baseline only (explicit bad credentials error; missing default still errors), C0c.2/.3/.4/.5/.6/.7/.8 remaining. #24815 existing-pane focus/input tests do not prove cold daemonless launch/adoption.
- #23240: C1.1 implemented typing subset, unconfirmed flag/fallback-state acceptance remaining; C1.2 source behavior present; C1.3/.4 remaining exact contract/status. No whole-leaf close.
- #23241: C2.1/.2/.3/.4 remaining; C2.5 safety gate present but unconfirmed/repeated-take completion remaining.
- #23242: C3.1 remaining authority semantics; C3.2 direct-first code present, specified contract/guide acceptance remaining; C3.3/.4 remaining; C3.5 re-register exists, unfocused retakes/unconfirmed clear remaining.
- #23244: D3.1/.2/.3/.4/.5/.6/.7/.8/.9/.10 remain unproven/incomplete. Existing spawn_commit-specific written tracking is not generic effect-bearing-request retirement. Current generic _close_generation can await cancellation and writer closure indefinitely; no aggregate grant/revoke budget covers runtime ensure/reconnect/retry.
- #23245: D3b.1 remaining (daemon_shutdown subset exists, ws_close differs, all relevant observer awaits still lock-held); D3b.2/.3/.4/.5/.6 remaining.
- #23247: E1.1 remaining at relocated code; E1.2 revise as above.
- #24103: criteria 1/.2/.3 remain missing; 4/.5 need candidate regression evidence after implementation. Sanitized pool logger routing already shipped independently.

**Primary evidence registry**

All `src:` identifiers below came from `gcode evidence`, using the audited binding above. Paths/lines refer to that snapshot. Reproduce any entry with:

```sh
gcode evidence --request-json '{"schema_version":1,"operation":"read","read":{"kind":"range","path":"PATH","start_line":START,"end_line":END}}'
```

Use the registry's contiguous ranges separately when an entry names several. Every inspected range returned complete source; overly broad requests were narrowed before use.

| ID | Current file:line | Observation | gcode evidence identity |
| --- | --- | --- | --- |
| E1 | `crates/gclient/src/app/live.rs:372,381,412-415` | Event handling awaits roster, attention and unresolved opens. | `src:fd07e5e27b1a5e9e8a4ae321f27989cb475a2df9c6852ef697052ef066972ec4` |
| E2 | `crates/gclient/src/app/live_workspace.rs:98-121` | Unresolved ids helper exists; serial open errors are discarded. | `src:164fde2326a7020c764a38ea7ab6124e103743cc319e2ab292171c281b396dae` |
| E3 | `crates/gclient/src/app/live_loop/reconnect.rs:54-74` | handle_live_event remains async and awaits apply_live_event. | `src:586ff6ea1084bd689185d7d8d3a2418c640b7d0f7581d20d10f94f568bfc9b26` |
| E4 | `crates/gclient/src/app/live_loop/jobs.rs:35-123` | Job keys/results have no Terminal/Open, Roster, Attention or committed-orphan chain family. | `src:19b4fc20b14da63e6b4978f60e35b5a397a8baf6f0e5fcda5c4c9dfd7197ebc4` |
| E5 | `crates/gclient/src/app/live_loop/lifecycle.rs:183-242` | Shell creation, owned-shell adoption, roster and attach still await inline. | `src:d79c4adef0110bc819b6e9e2f84d7fd4676f7fc7a7e80e070983b54ca6dd9a29` |
| E6 | `crates/gclient/src/app/live_loop/lifecycle.rs:149-154; crates/gclient/src/app/live_loop/daemon_ops.rs:96-140` | close_live_tab delegates job; kill_live_panes issues TabClose only with no kept panes, after kills. | `src:a9ec9ab59a5a80eab0076e9ff88c442f5a4c5a2ee4a2245b15de79ef63a13ce3` |
| E7 | `crates/gclient/src/app/live_loop/arrange.rs:14-38` | Explicit comment and send_op await leave arrange/grid conversion to A4b. | `src:28d591b1bf50a26a9c9783ee4a7f9b6672f09ebbb53077d1ad33a1909a69e782` |
| E8 | `crates/gclient/src/app/live_loop/orphans.rs:137-218` | Dialog inventory awaits; kills are sequential with ? short circuit. | `src:99fb44ecd74b614902e90298eec73588880d9124292e1d8a798c4b3b844ce725` |
| E9 | `crates/gclient/src/app/live_loop.rs:252-270,434-490` | Startup stages, relist and reconnect futures are selected beside loop. | `src:252447643baac7f9d14447274e496960d9b65fb93b8db0aa4599ec112bddc644` |
| E10 | `crates/gclient/src/daemon/live.rs:40-95` | LiveState stores bare oneshot reply senders, no issuance timestamps. | `src:5f0d7d123eeef9095136cb576546f5f3ccd981de0c074a2b68fea3245ad4992d` |
| E11 | `crates/gclient/src/app/live_sidebar.rs:246-266; crates/gclient/src/app/live_loop.rs:561-569` | Poll due checks and issuing guards lack Slow health state. | `src:ebee36024e08ab8a4402bfa5f660ae38c48d41414706b3bbf9562fd86426ed50` |
| E12 | `crates/gterminal/src/protocol/wire_types.rs:216-291,518-562` | Complete ClientMessage and ServerMessage enums have no native inventory request/response. | `src:bfbfa7eae059446ee6f63368091ef61e34f7a9f13b0bc6c71a28eef2ed49835e` |
| E13 | `crates/gterminal/src/host/write.rs:94-185` | grant_input simply replaces input_grant; no conn_id fence; frame_input matches only the bound attachment string. | `src:7331f263b648b37aa68a1b24b1e42066c3c320330266b11f9985186a4081094c` |
| E14 | `crates/gclient/src/frame_source.rs:41-48; crates/gclient/src/frame_source/ tree; crates/gclient/src/app/ tree` | Indexed structure has only proxy.rs/tests.rs under frame_source; no native_host.rs/native_degraded.rs. Full wire enums corroborate missing inventory path. | `gcode tree/outline; no repository-wide negative inferred from search alone` |
| E15 | `crates/gclient/src/startup.rs:432-462` | Default API-key read failure and empty credential both become TokenFile error. | `src:3212ef6c83293bfae0eeb804f65c46ffda0f912416f0c3958e126aeffee7826c` |
| E16 | `crates/gclient/src/app/disconnect.rs:34-46` | Writable direct panes retain control; every other pane clears it. | `src:5e152ed5558f3a69929bbbc25c47c5695fa71fa8d6235fdf30a36b8b5cd4305f` |
| E17 | `crates/gclient/src/app/live_loop/control.rs:397-466` | Existing direct-native panes can queue input and request a host grant during outage. | `src:6ab69dd7cab6dd252a649c05e2e47730967766bf90ceb9b0659205ee91dfa98b` |
| E18 | `docs/guides/gclient-user-guide.md:618-637,647-665` | Guide describes direct input and explicit grant failures, but lacks required unconfirmed-lease text. | `src:4771ba3917bbd8c45d1585ed007f92faac15c92fc404e72a6139addaee135f6e` |
| E19 | `crates/gclient/src/app/live_loop/control.rs:135-178` | Granted lease without host grant does not flush input; current response asks manual take-back. | `src:97d0c097c6df02779124af27898a0044cebce43f0f675b0086fca9f3ebe29316` |
| E20 | `crates/gclient/src/app/live_attach/recovery.rs:310-339,459-482` | Host reregister adopts new attachment, then requests control only for focused pane. | `src:678111d1e4f5a23c8897103072ab10874182ce0d345837f89a53d42bb89821b6` |
| E21 | `crates/gclient/src/app/live_loop/host_recovery.rs:268-339` | Direct source restores host control immediately; failed local recovery falls back through daemon. | `src:6d6fea25ab784ed57e01f1756d2e4b0261214f4ee336a3f15889c881b816a176` |
| E22 | `src/gobby/terminals/host_client.py:351-470` | Cleanup awaits reader and writer closure; roundtrip has no timeout; operation lock has no budget. | `src:effdd3cd91a07cce36d9b388af10893ce31b0f8aa8aeea6d06344c5cb152dd15` |
| E23 | `src/gobby/terminals/native_runtime.py:650-680` | Ensure, grant/revoke and reconnect/retry have no shared deadline. | `src:a5ee7e03a3a25041747d46bdca596852dbb362b8e4527e915ef304be23e1403a` |
| E24 | `src/gobby/terminals/leases.py:493-507,514-600` | Repeated take, take, release and finalize await observer while owning lease lock; preserve branch only daemon_shutdown. | `src:d582a2ebea063a52dbc30d5e12c62f09e7f61584a7bee7653a190a8f6be1ae04; src:c0bef3d66a52dbf40cf9bb8ec5f2de6fc12d6a3c859954e5082e28bedf46f9db` |
| E25 | `src/gobby/servers/websocket/terminal_ws.py:577-588; src/gobby/servers/websocket/terminal_ws_control.py:39-67` | Cleanup distinguishes daemon_shutdown from ws_close; take handler awaits registry then sends host_input_granted. | `src:6cc49fed29a1e3538d43f04c18ba15a6bd6fdb9f06abcff8617cc1cecb0224b2` |
| E26 | `src/gobby/terminals/input_grants.py:53-102` | daemon_shutdown records durable handoff; normal absent holder revokes and direct holder grants. | `src:ed3cc8dfb6155d7cc0267808de13ad091dc55dd2ab3bffe4a35c99d169310d98` |
| E27 | `src/gobby/terminals/native_frames.py:109-117` | reserve_observer still subscribes the main client once. | `src:88b41411263b5760bf56b158039ae1e7bb802c7ccee12822e67eb53f39917137` |
| E28 | `src/gobby/terminals/host_event_reader.py:32-39` | Actual event consumer uses HostClient.open_event_stream on its own connection. | `src:b71d7348083643d7c2c3f5c64ebf848e1cc8b8e92485aec01bd06deeab40ec60` |
| E29 | `src/gobby/storage/hub/postgres_pool.py:194-223` | Final acquire failure logs pool_stats and raises; no census scheduling. | `src:9aed55a3ad71f682f073baf446b2e81556aa62b267e7c5a03f4f316a22ece555` |

**Acceptance artifact inventory**

An indexed regex search for every named test from the 17 criteria returned `complete_empty` at the audited binding (request fingerprint `a2845be15ddab2916c92b6180fa4e127b56e4095d6383215136788a8ad8c85d2`). Its explicit warning says search absence alone is not a repository-wide negative. A second exact tracked-content check (`git grep -n -E '<all criterion test names>' HEAD -- crates/gclient crates/gterminal tests`) returned no matches (exit 1). This establishes absent exact named test artifacts, not absent equivalent coverage.

Equivalent existing tests include `crates/gclient/tests/host_upgrade_recovery.rs:94` (host reconnect without daemon), `:210` (held native input through outage), `:296` (new grant required during daemon reattach), `:453` and `:541` (fallback and budget), `:764` (generation change), `:857` (fresh attachment before take). `outage_direct_input.rs:273` exercises outage focus/grant typing; `:357` waits on grant; `:411` refusal; `:575` proxy input status; `:713` daemon-return adoption of an outage grant. These are source-inspected test artifacts, not test executions in this seat.

**Recorded acceptance criteria (exact task text before generic artifact/TDD boilerplate)**

**#23230 Open unresolved terminals as jobs**

A3b.1: A held unresolved-terminal open keeps frames, ticks and direct input progressing, installs the terminal once after release, and coalesces repeated workspace events. test: `crates/gclient/tests/loop_liveness.rs::a_held_open_unresolved_terminal_job_is_coalesced_and_nonblocking`.
A3b.2: The inline reconcile helper remains available while the live workspace event path issues `Opened` jobs without awaiting. symbol: `open_unresolved_terminals`.
A3b.4: A failed unresolved-terminal open emits one WARN with terminal, generation, error class and `elapsed_ms`; the error is no longer discarded. test: `crates/gclient/tests/loop_liveness.rs::an_unresolved_terminal_open_failure_is_logged_with_elapsed_time`.

**#23231 Event-driven refetches and roster jobs**

A4a.1: A roster for a project no longer focused is dropped (was A4.3). test: `crates/gclient/tests/loop_liveness.rs::a_roster_for_a_project_no_longer_focused_is_dropped`.
A4a.2: `apply_live_event` is sync and sets refetch flags instead of awaiting (was A4.5). symbol: `apply_live_event`.
A4a.3: Failed REST components and both `Lagged` entry paths emit WARN records with operation/source, generation, error class or skipped count, and `elapsed_ms`, while successful partial rows still apply (was A4.8). test: `crates/gclient/tests/loop_liveness.rs::rest_failures_and_lagged_events_warn_with_elapsed_time`.

**#23232 Lifecycle and shell-adoption actions as chained jobs**

A4b.1: A held `terminal_create` keeps the window interactive and places the pane after the create lands (was A4.1). test: `crates/gclient/tests/loop_liveness.rs::a_held_terminal_create_keeps_the_window_interactive_and_places_after_create`.
A4b.2: Closing a tab issues `TabClose` only after every kill settles (was A4.2). test: `crates/gclient/tests/loop_liveness.rs::closing_a_tab_issues_tab_close_only_after_every_kill_settles`.
A4b.3: Two new tabs before the first create lands place both (was A4.4). test: `crates/gclient/tests/loop_liveness.rs::two_new_tabs_before_the_first_create_lands_place_both`.
A4b.4: Splitting right while the old connection's release/take or terminal-create reply is held keeps direct input, frame ingest and rendering live; the placement is applied only after its own job settles (was A4.6). test: `crates/gclient/tests/loop_liveness.rs::split_right_with_a_held_control_or_create_reply_keeps_the_window_live`.
A4b.5: A held `TabCreate` in the gclient-owned shell adoption chain keeps frames, ticks and direct input live; the chain resumes from each outcome, and a refused step rolls back the adopted tab (was A4.9). test: `crates/gclient/tests/loop_liveness.rs::a_held_adoption_step_keeps_the_window_live_and_rolls_back_on_refusal`.
A4b.6: A `terminal_create` reply that lands after the loop's generation changed kills the unplaced terminal exactly once after the new workspace installs and places no pane. A create whose connection is lost before its reply is neither replayed nor compensated. test: `crates/gclient/tests/loop_liveness.rs::a_stale_committed_create_is_compensated_once_and_an_unanswered_create_is_not_replayed`.
A4b.7: An adoption `TabCreate` reply that lands after its target project closed runs one rollback `TabClose` for the captured tab. test: `crates/gclient/tests/loop_liveness.rs::a_stale_committed_adoption_step_rolls_back_its_captured_tab`.
A4b.8: A stale create reply is disposed exactly once in both orders: arriving after the new generation's model is installed, it is killed at once; arriving before, it is killed when that model installs. A disposal kill whose reply is lost keeps its entry and raises one toast. The next install re-evaluates the entry and clears it once the terminal is gone. test: `crates/gclient/tests/loop_liveness.rs::a_stale_create_is_disposed_once_in_either_order_and_a_failed_disposal_stays_owned`.
A4b.9: A held arrange or create-grid `workspace_op` keeps frames flowing, and the grid chain places panes from the returned ids. test: `crates/gclient/tests/loop_liveness.rs::a_held_arrange_or_grid_op_keeps_frames_flowing_and_places_from_returned_ids`.

**#23233 Orphan inventory and destroy fan-out as jobs**

A4c.1: Holding orphan inventory leaves the dialog and window live; after release, orphan kills fan out concurrently and `DestroySummary` preserves one result per requested orphan regardless of completion order (was A4.7). test: `crates/gclient/tests/loop_liveness.rs::held_orphan_fetch_and_kill_fanout_are_nonblocking_and_complete`.

**#23234 Launch and reconnect audit and WARN bracket**

A5.2: A reconcile from a stale generation is dropped. test: `crates/gclient/tests/loop_liveness.rs::a_reconcile_from_a_stale_generation_is_dropped`.
A5.4: A bounded final audit enumerates launch/reconcile, control, input, daemon-event, frame-recovery, sidebar, resize/tick attach, focus-hint, geometry, attention, unresolved-open, roster/orphan, lifecycle-action and shell-adoption paths; every daemon-touching `run_live_loop` branch or post-select helper maps to a held-request case that advances frames, ticks and unaffected direct input. test: `crates/gclient/tests/loop_liveness.rs::every_run_live_loop_daemon_path_is_issued_without_awaiting`.
A5.5: Each reconnect/reconcile attempt emits exactly one start and one finish WARN with reason, generations, outcome and `elapsed_ms`, including success, timeout and transport-loss cases. test: `crates/gclient/tests/loop_liveness.rs::reconnect_attempts_warn_once_at_start_and_finish_with_reason`.
A5.6: With `GET /api/agents/runs` held, a prefix chord toggles the sidebar immediately and applies once. test: `crates/gclient/tests/loop_liveness.rs::a_chrome_chord_applies_while_a_rest_request_is_held`.

**#23235 Daemon health from in-flight age**

B1.1: Every waiter records `issued_at` and `oldest_inflight_age` reports the oldest. symbol: `LiveState`.
B1.2: `DaemonHealth` derives `Slow` past the threshold and `Unreachable` on disconnect. test: `crates/gclient/src/app/live_loop/health/tests.rs::health_is_slow_once_the_oldest_in_flight_request_passes_the_threshold`.
B1.3: The status line shows `Daemon slow` after 1 s of a held `workspace_op` and clears when the reply lands. test: `crates/gclient/tests/loop_liveness.rs::a_slow_workspace_op_reply_shows_daemon_slow_until_it_lands`.
B1.4: The guide's status-line section describes the slow state. behavior: "Daemon slow" in `docs/guides/gclient-user-guide.md`.

**#23236 Shed load while slow**

B3.1: No sidebar `/api/source-control/status` or roster refresh request is recorded while the daemon is slow, and polling resumes on `Ready`. test: `crates/gclient/tests/loop_liveness.rs::sidebar_polls_pause_while_the_daemon_is_slow_and_resume_on_ready`.
B3.2: The guide's status-line section says polling pauses while slow. behavior: "pauses" in `docs/guides/gclient-user-guide.md`.

**#23237 Native inventory and fallback input authority in gterm**

C0.1: An authenticated frame client lists only committed live native terminals with stable terminal/host ids and host epoch; unauthenticated and tmux inventory is unavailable. test: `crates/gterminal/tests/frame_protocol.rs::daemonless_native_inventory_is_authenticated_and_excludes_tmux`.
C0.2: With no control owner or grant, the first bound local attachment can type and paste; a second attachment cannot replace it and receives `input_not_granted`. test: `crates/gterminal/tests/frame_protocol.rs::first_local_attachment_holds_fallback_input_until_detach`.
C0.3: Detaching clears its local fallback, while detaching another frame stream cannot clear either the fallback or a daemon grant. test: `crates/gterminal/tests/frame_protocol.rs::frame_detach_clears_only_its_local_fallback_grant`.
C0.4: A control owner disables new fallback authority; its `grant_input` replaces an existing fallback, and matching/unconditional revoke clears the resulting holder. test: `crates/gterminal/tests/control_protocol.rs::daemon_grant_replaces_local_fallback_and_restores_arbitration`.
C0.5: The protocol contract and wire goldens describe the authenticated native inventory and local-fallback/daemon-takeover rules. behavior: "local fallback holder" in `docs/contracts/gterm-protocols.md`.
C0.6: Last-control-owner disconnect transfers a bound daemon grant to that same frame connection; rebinding it to the fresh reconnect attachment id preserves input until the next daemon grant replaces the fallback. test: `crates/gterminal/tests/control_protocol.rs::disconnect_and_rebind_transfer_the_daemon_grant_without_an_input_gap`.
C0.7: Over two real control connections, a `grant_input` sent on the older connection after the newer one revoked or granted the same terminal is refused `stale_grant_connection` and leaves the newer holder in place. The same late request before any newer authority change applies and is then replaced. test: `crates/gterminal/tests/control_protocol.rs::a_late_grant_from_a_retired_connection_cannot_overwrite_newer_authority`.

**#23238 Read native inventory through the existing frame socket**

C0b.1: `NativeHost` uses the existing authenticated frame codec and returns typed native inventory without any daemon request. file: `crates/gclient/src/frame_source/native_host.rs`.
C0b.2: Against a real gterm with no lasting control connection, gclient lists a native terminal, attaches it and receives frames within one 2 s aggregate local deadline. test: `crates/gclient/tests/frame_source_live.rs::native_inventory_and_attach_work_without_a_daemon`.
C0b.3: Missing, refused and timed-out local-host probes return typed outcomes and leave no reader/writer task or socket alive. test: `crates/gclient/tests/frame_source_live.rs::native_inventory_failure_cleans_up_the_frame_connection`.

**#23239 Launch, render and type native panes with no daemon**

C0c.1: A missing default daemon token produces an empty degraded Ready window, an unreachable daemon with a valid local token can use the native host, and an explicitly requested bad token file remains a startup error. test: `crates/gclient/tests/startup.rs::missing_default_daemon_token_starts_in_native_degraded_mode`.
C0c.2: With the daemon absent and a real gterm present, gclient launches, lists native panes only, attaches, renders advancing frames and delivers typed bytes to the PTY. test: `crates/gclient/tests/loop_liveness.rs::daemonless_launch_attaches_renders_and_types_native_panes`.
C0c.3: A competing local gclient that receives `input_not_granted` becomes read-only without daemon fallback, while the first holder keeps typing. test: `crates/gclient/tests/loop_liveness.rs::daemonless_second_client_cannot_take_over_the_local_holder`.
C0c.4: When the daemon connects after degraded launch, matching terminal ids preserve one direct source, daemon layout replaces local tabs, the adopted holder stays Held with `lease_unconfirmed` set, and no duplicate pane appears. test: `crates/gclient/tests/loop_liveness.rs::daemon_reconcile_adopts_daemonless_native_panes_without_duplication`.
C0c.5: Without the daemon, the UI states the unavailable enhancements and never lists tmux, web or proxied panes. behavior: "Native degraded mode" in `docs/guides/gclient-user-guide.md`.
C0c.6: With two native terminals and no daemon, gclient opens one independently attached direct pane per inventory row; frames advance and typed bytes reach the correct PTY on both streams. test: `crates/gclient/tests/loop_liveness.rs::daemonless_launch_attaches_and_types_multiple_native_panes_independently`.
C0c.7: With the daemon absent, focus moves A to B to A by keyboard and by mouse without any daemon release or take, both panes stay Held, and typed bytes reach both PTYs. An explicit take-back answers the reconnect status. test: `crates/gclient/tests/loop_liveness.rs::daemonless_focus_moves_between_direct_panes_and_types_into_both_ptys`.
C0c.8: A native inventory result, or a native attach result, held until after the daemon workspace installs is disposed: every frame connection it holds is closed and no duplicate pane appears. The same holds when the terminal was removed first, and when the loop exits while the job holds sources. test: `crates/gclient/tests/loop_liveness.rs::a_late_native_result_after_the_daemon_workspace_is_disposed_and_closes_its_sources`.

**#23240 Keep control on disconnect for direct-granted panes**

C1.1: A daemon-granted or local-fallback direct pane stays Held with `lease_unconfirmed` when the mock daemon closes the socket, and the mock frame host still receives `Input`. test: `crates/gclient/tests/loop_liveness.rs::a_direct_granted_pane_keeps_typing_through_a_daemon_disconnect`.
C1.2: A proxied pane drops to Observe on the same disconnect. test: `crates/gclient/tests/loop_liveness.rs::a_proxied_pane_drops_to_observe_on_daemon_disconnect`.
C1.3: The protocol contract states the new outage contract. behavior: "keeps typing" in `docs/contracts/gterm-protocols.md`.
C1.4: The guide's status-string table carries the unconfirmed-lease text. behavior: "typing continues on the host" in `docs/guides/gclient-user-guide.md`.

**#23241 Reconfirm the lease on reconnect**

C2.1: On reconnect every unconfirmed direct pane rebinds its preserved frame stream before `terminal_take_control`; bytes typed before the reply continue to reach the host, and `granted` clears the unconfirmed state without duplicating the source. test: `crates/gclient/tests/loop_liveness.rs::an_unconfirmed_direct_stream_rebinds_and_keeps_typing_until_retake_grants`.
C2.2: `lease_unconfirmed` clears on grant and a `held` reply drops the pane to Observe with take-back. symbol: `Pane`.
C2.3: After a daemonless launch (C0c), the daemon's arrival retakes every adopted unconfirmed holder: each rebinds its preserved stream and issues one take, typing continues until the grant, and no duplicate source appears. test: `crates/gclient/tests/loop_liveness.rs::daemon_arrival_after_degraded_launch_retakes_every_adopted_holder`.
C2.4: Each of three unconfirmed panes issues its own take. One take fails while the daemon socket stays healthy; it is retried at most three times and then drops that pane to Observe with take-back, while the other two confirm. test: `crates/gclient/tests/loop_liveness.rs::every_unconfirmed_pane_retakes_and_one_failed_take_retries_boundedly`.
C2.5: `granted: true` with `host_input_granted: false` keeps `lease_unconfirmed` set and claims no typing authority until a repeated take confirms the host grant. test: `crates/gclient/tests/loop_liveness.rs::a_lease_grant_without_a_host_grant_does_not_confirm_typing`.

**#23242 Direct re-attach before proxy fallback**

C3.1: A host-local re-attach keeps the pane Held and typing on the new stream, sets `lease_unconfirmed` and shows `Reconnected; input authority unconfirmed.` instead of confirming the grant. test: `crates/gclient/tests/host_upgrade_recovery.rs::a_host_local_reattach_marks_input_authority_unconfirmed`.
C3.2: The contract and guide state direct-first recovery. behavior: "direct re-attach" in `docs/contracts/gterm-protocols.md`.
C3.3: With the daemon absent, a fallback holder's stream fails and the replacement stream reclaims fallback, so typed bytes reach the PTY. If a peer local client claimed first, the replacement is refused and drops to Observe. test: `crates/gclient/tests/loop_liveness.rs::a_lost_fallback_stream_reclaims_only_when_no_peer_holds_input`.
C3.4: A fallback stream lost after the daemon has returned, but before C2's retake grants, cannot reclaim fallback. Its input is refused until the retake grants the fresh attachment id; after that, bytes reach the PTY. test: `crates/gclient/tests/loop_liveness.rs::a_fallback_stream_lost_during_daemon_return_types_only_after_the_retake`.
C3.5: After the daemon returns on a new generation, every unconfirmed host-recovered pane, focused or not, issues its own take once its re-register lands, and each grant clears `lease_unconfirmed`. test: `crates/gclient/tests/host_upgrade_recovery.rs::every_unconfirmed_host_recovered_pane_retakes_after_reregister`.

**#23244 Bounded HostClient round trips**

D3.1: A round trip past its deadline raises `HostUnavailableError("timed out")` and drops the pending future. test: `tests/terminals/test_host_client.py::test_roundtrip_times_out_with_host_unavailable`.
D3.2: Grant and revoke use one absolute 1.5 s budget across ensure, a first attempt ending in EOF, reconnect and the second attempt. With the retry stalled, wall time stays below 2 s, every HostClient pending entry is removed, and a competing acquisition of the same `TerminalLeaseRegistry.lock(terminal_id)` succeeds by that original deadline. test: `tests/terminals/test_native_runtime.py::test_eof_reconnect_and_retry_share_one_deadline_and_release_the_lease_lock`.
D3.3: `control_timeout_seconds` defaults to 5.0 and is exported to the runtime config contract. test: `tests/config/test_terminal_host_config.py::test_control_timeout_seconds_default`.
D3.4: Validation treats the regenerated gcore carrier as an input change to the coherent trio and records the batched installed identity/hashes from `~/.gobby/bin/` plus separate gclient promotion. file: `crates/gcore/assets/config/runtime_config_contract.json`.
D3.5: The host process helpers live in their own module and `host_manager.py` is under 850 lines. file: `src/gobby/terminals/host_process.py`.
D3.6: A mutating request that times out, including time spent waiting for `_operation_lock`, retires the connection. The next mutation runs on a new connection from `operation_seq` 1 with no `operation_conflict`, and the timed-out request is not re-sent. test: `tests/terminals/test_host_client.py::test_mutating_timeout_retires_the_connection_and_never_reuses_its_sequence`.
D3.7: A grant whose fake-host reply is withheld past its deadline retires the connection, and the following revoke is issued on a new connection. test: `tests/terminals/test_host_client.py::test_grant_timeout_retires_the_connection_before_the_next_authority_change`.
D3.8: A grant is written and the fake host parks it at a barrier before applying it. The fake host enforces C0's per-slot connection fence, whose real-host rule C0.7 proves. The caller is cancelled before its deadline. That cancellation retires the connection, and the recovery revoke is issued on a newer connection. Releasing the parked grant afterwards leaves the revoke in force. A request cancelled before it is written leaves the connection in place. test: `tests/terminals/test_host_client.py::test_cancelling_a_written_grant_retires_the_connection_so_newer_authority_survives`.
D3.9: In this test the old writer's `wait_closed` never returns and its reader ignores cancellation. Retirement still returns without awaiting either, and the detached cleanup ends at its bound. The grant's aggregate budget holds, and a reconnect installed while the stale cleanup runs keeps its reader, writer and generation after that cleanup ends. It also covers an old request's timeout or cancellation handler that runs after another caller has reconnected: its retirement names the old generation and leaves the new connection open. test: `tests/terminals/test_host_client.py::test_retirement_fences_before_cleanup_and_stale_cleanup_leaves_the_new_connection`.
D3.10: A timed-out grant, revoke, resize, kill or `terminate` is not re-sent after the runtime reconnects. In `_write`, a timed-out host write, or one whose connection is lost after its bytes were written, returns `IndeterminateWrite`. That includes the text+submit path when the loss hits either the text or the `enter` write. A loss before the first write stays `TerminalWriteError(stage="none")`. An EOF inside the budget still gets its one retry. A timed-out `write_batch` reports `IndeterminateWrite` for every target. test: `tests/terminals/test_native_runtime.py::test_abandoned_requests_are_never_replayed_and_timed_out_writes_are_indeterminate`.

**#23245 Host grant reconciliation never holds the terminal lease lock**

D3b.1: The transition matrix sees `lock_held(terminal_id) == false`; direct `ws_close` preserves the current host grant, while repeated/first take, takeover, explicit release, detach, terminal removal and non-direct cleanup replace or revoke it. test: `tests/terminals/test_lease_authority.py::test_holder_transition_matrix_applies_preserve_and_revoke_policies_outside_the_lease_lock`.
D3b.2: A stalled older grant cannot overwrite a newer takeover or final revoke; after the stall releases, the last host notification matches the latest generation. test: `tests/terminals/test_lease_authority.py::test_stalled_holder_sync_converges_to_the_latest_generation`.
D3b.3: With a real WebSocketServer, HostClient and gterm, restarting the isolated daemon finalizes its websocket state without revoking the direct grant; `Input` typed after cleanup starts and while the reconnect take is paused still reaches the PTY, then C2 replaces the grant and an explicit detach refuses later input. test: `tests/e2e/test_terminal_client_stack.py::test_direct_native_input_survives_daemon_restart_until_lease_retake`.
D3b.4: A test barrier holds an actual `HostClient` grant after request registration; while it is pending, `lock_held(terminal_id) == false`, another connection lane and same-terminal lease-state acquisition complete, and D3's deadline clears the pending request without any production watchdog. test: `tests/servers/test_terminal_ws_lease.py::test_unanswered_host_grant_is_bounded_and_lock_free`.
D3b.5: In the barrier race where the newer normal sync wins the holder-sync lock before recovery, recovery snapshots only after acquiring that lock and the final host state still matches the newest generation. test: `tests/terminals/test_lease_authority.py::test_reconcile_latest_holder_snapshots_under_the_sync_lock_after_newer_sync_wins`.
D3b.6: An older take's observer is held while a newer takeover or finalize commits. The older reply carries its own captured generation and sizing with `host_input_granted=False`, sizing is never recomputed outside the lease lock, and the final host state matches the newer generation. test: `tests/terminals/test_lease_authority.py::test_a_superseded_take_reply_keeps_its_captured_generation_and_claims_no_host_grant`.

**#23247 Drop the undrained host event subscription**

E1.1: `reserve_observer` sends no `subscribe_events`. test: `tests/terminals/test_native_runtime.py::test_reserve_observer_sends_no_subscribe_events`.
E1.2: The spawn-failure helpers live in their own module and are re-exported. file: `src/gobby/terminals/native_spawn_failure.py`.

**#24103 Recover #23246 pool exhaustion census: rate-limited nonblocking pg_stat_activity census on final pool acquire failure**

1. test: tests/storage/hub/test_postgres_pool_backoff.py::test_final_acquire_failure_logs_a_rate_limited_activity_census passes: the final acquire failure logs one pg_stat_activity census, and concurrent failures within a minute run at most one.
2. test: tests/storage/hub/test_postgres_pool_backoff.py::test_census_failure_reason_is_typed_and_claims_capacity_only_for_53300 passes: capacity exhaustion is claimed only for SQLSTATE 53300, and no log line carries exception text or the DSN.
3. test: tests/storage/hub/test_postgres_pool_backoff.py::test_census_is_bounded_and_preserves_the_original_acquisition_error passes: the caller gets the original PoolTimeout at once whether the census succeeds, fails or stalls, a stalled census blocks a second one, and the census connection carries connect_timeout, statement_timeout and keepalives.
4. Each new test fails at the cut base and passes at the candidate. The existing D4.2 psycopg.pool sanitized routing test still passes.
5. Focused protected pytest for tests/storage/hub/test_postgres_pool_backoff.py and tests/telemetry/test_logging.py passes on the test hub with GOBBY_TEST_PROTECT=1; ruff and mypy clean on touched src files.

### Risks

- This is a static validity audit, not a green validation receipt. No cargo build, Rust suite, pytest or live-daemon interruption was run. Closure still requires implementation evidence and scoped validation; no historical TDD receipts were inferred.
- Daemon outage versus cold daemonless launch are different contracts. Closing C0/C0b/C0c from #24815's existing-pane behavior would leave native discovery and launch missing.
- Current outage grant acquisition uses host control and can replace a grant; the planned fallback contract requires first local holder exclusivity with no local takeover. Retain/test that behavioral requirement when choosing how to integrate existing mechanisms. No whole mechanism or safety requirement is declared obsolete here.
- The normal ws_close revoke path and lock-held host calls can interrupt the source-side outage behavior. Client-only success against a mock that preserves grants is insufficient for D3b.3.
- The generic host-request deadline and control-connection fence must be designed and validated together; adding a timeout without retiring possibly written effects allows a late abandoned grant to overwrite newer authority.
- Do not close #24103 by linking the unreachable #23246 census commit. Reimplement only the missing census on the current logging/deadline policy; preserve the independently shipped sanitizer.
- Integration may advance after this snapshot. Recheck changed audited paths before applying any proposed task rescope or close. No operator decision is required to retain these open safety/behavior requirements; any decision to drop first-holder exclusivity or all-pane reconfirmation belongs through the Assistant.

