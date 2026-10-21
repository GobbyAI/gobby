# Pane Focus Wake Gating

Plan artifact: `.gobby/plans/pane-focus-wake-gating.md`

**Plan ID:** pane-focus-wake-gating

## Overview
`kind: framing`

Task #24770 (Plan: pane-focus-gated operator comms wakes). Input: research
report `.gobby/plans/research/pane-focus-wake-gating-2026-10-21.md` (#24769,
researcher gobby#17246). Baseline: #24768 (landed `c2714e4a5c`, `9f76e9f6e2`)
already wipes an idle attached seat's composer when an authenticated operator
comms wake (a Telegram DM to that seat) is dispatched.

Problem: that wipe fires the moment the DM arrives, even while Josh is typing
in the seat's gclient pane. Josh's rule (Telegram, 2026-10-20 23:19 CDT):
"When a pane has focus, hold wake messages in queue. When the pane loses
focus, the pane's composer can be wiped by the daemon and the wake message
sent. Pane focus state would clear after x minutes after the last [activity]."

Outcomes:

- The daemon knows, per attachment, whether the lease holder's pane is
  focused, in gclient or in the web terminal, through one new websocket verb,
  and clears it when the lease ends or is taken over (1.1).
- Operator comms wakes to a seat whose terminal is held by a focused
  attachment of either viewer are withheld as `pane_focused` and
  re-dispatched, with the existing #24768 wipe, the moment focus ends (1.2).
- gclient owns its idle clock: it reports focus when it holds the focused
  pane, and reports focus lost after `focus_hold_seconds` (default 300,
  configurable) without user activity in that pane (2.1).
- The web terminal sends the same report with the same idle clock: focus once
  it holds the lease, focus lost after `focusHoldSeconds` (default 300,
  configurable in Settings) without activity, and switching to another
  application no longer drops its lease (3.1).

Task #24770 criterion 1 names four leaves. This plan carries them in three:
the daemon per-attachment focus state is 1.1, the wake-dispatch hold and
release is 1.2, and the gclient focus signal and its idle-timeout expiry are
both 2.1, because one gclient struct owns the report and the clock, and a
signal shipped without its expiry would hold wakes indefinitely. Task
#24784 adds 3.1, the web terminal's report and expiry, under Josh's
2026-10-21 ruling that web viewers count as focused (decision 22). Each leaf
names its tests in its acceptance items and states its activation class.

Activation classes: P1 is daemon code and lands with a daemon restart. P2 is
a gclient crate change and lands with a gclient rebuild and install through
`promote_workspace_binary_set`; gclient is promoted separately from the
coherent `gcode`/`gdaemon`/`ghook` set. P3 is web UI code and lands with
`uv run gobby ui build` and a browser reload; it needs no restart and no
binary install.

## Decision Record
`kind: framing`

Josh's rulings (task #24770 description, Telegram 2026-10-20):

1. 23:19: hold wake messages while a pane has focus; on focus loss the daemon
   wipes the composer and sends the wake; focus clears x minutes after the
   last activity.
2. 23:21: "So that would be a change to gclient."
3. ~23:40: "300s sounds like a good configurable default."
4. ~23:45: "I don't think we need a grace delay. Also, when focus is acquired
   clicking outside of gclient (like me responding to an email) shouldn't
   cancel focus. User clicks a pane it's focused until it times out from user
   activity." No focus-loss grace delay. OS window blur does not cancel focus.
   Focus ends only by idle timeout from user activity or by focusing another
   pane.
5. Researcher recommendation accepted by Josh: gclient owns the idle timer in
   memory; the daemon stores a boolean focus state per attachment.
6. Active-turn protection stays first; the landed #24768 wipe is unchanged at
   dispatch.

Planner decisions:

7. **No DECSET 1004, no OuterFocus handling.** Ruling 4 makes window focus
   irrelevant. gclient already drops `RawInputEvent::OuterFocusGained` and
   `OuterFocusLost`; nothing changes there. The research report's Option B is
   dropped.
8. **One verb, `terminal_focus`.** Client to daemon:
   `{type: "terminal_focus", terminal_id, attachment_id, focused: bool}`. No
   reply. It is a state report, idempotent, and a stale attachment is ignored.
   It joins `_OFF_LOOP_MESSAGE_TYPES` so it keeps per-terminal arrival order
   with `terminal_take_control` and `terminal_release_control`, which the
   daemon processes off the read loop in per-terminal order (#22709).
9. **Focus-held predicate** (daemon): the terminal's lease holder is a live
   attachment with `focused` true, whatever its viewer (decision 22). gclient
   (2.1) and the web terminal (3.1) both send the report. A holder that is
   not focused never holds a wake. The daemon clears `focused` when the
   attachment releases the lease, is displaced by a takeover, or is
   finalized, so a stale report cannot outlive the lease.
10. **Rejected: idle expiry releases the lease instead of a focus flag**
    (researcher follow-up, relayed by the Orchestrator 2026-10-21). It reuses
    `terminal_release_control` and needs no daemon field or verb, but it
    turns the focus timeout into a write-authority drop: `release_live_control`
    sets the pane to `ControlState::Observe` (status bar `○ observe`), the
    holder observer revokes the gterm input grant, and the first keystroke
    after idle round-trips through a queued `takeover: true` take instead of
    landing directly on the host stream. Josh ruled on none of that, and task
    criteria 1 and 2 require the per-attachment boolean. Rejected.
11. **Rejected: a daemon-side clock.** It duplicates state gclient has
    (ruling 5) and needs a timer and a wall-clock stamp the lease registry
    does not keep.
12. **Release trigger is a direct re-dispatch**, not a kick of the sleeping
    retry. `_retry_withheld_wake` sleeps with `asyncio.sleep` through
    `_composer_retry_wait(delay)`, which seven tests replace by name, so its
    signature stays. `WakeDispatcher.release_focus_hold(terminal_id)` pops the
    sessions withheld on that terminal and schedules
    `dispatch_live_wake(session_id, priority=..., require_unread=True)` as a
    task on the owner loop. `dispatch_live_wake` gains that keyword and passes
    it to `_dispatch_live_wake_unlocked` (~507), whose existing check
    (~512-520) returns `skipped: "mailbox_read"` when the session has no
    undelivered message, as the backoff loop already does (~372-374): a DM
    the operator's own turn read while the hold stood is never replayed with
    a wipe. The per-session live-wake lock serializes the release against a
    retry in flight. The backoff retry stays as the fallback if an event is
    lost.
13. **Nothing dispatches under the lease lock.** `_notify_holder` runs inside
    `TerminalLeaseRegistry.lock(terminal_id)`. The focus observer it calls is
    synchronous and only creates tasks.
14. **Hold scope: operator comms wakes only.** `is_operator_comms_wake`
    (#24768) decides which pending messages count. Agent-to-agent wakes keep
    today's draft protection and are not held by focus: they never wipe.
15. **Check order in `_composer_blocks_wake`:** probe error, open transcript
    turn (`turn_in_flight_fingerprint`), then the new `pane_focused` hold,
    then the #24768 comms branch, then the ledger state. The `active` session
    row check in `_dispatch_live_wake_unlocked` and `wake_batch.py` stays
    ahead of all of them.
16. **gclient activity** is any key, text or paste event, or any mouse event
    other than pure pointer motion (`MouseEventKind::Moved`), that the live
    loop receives, whether it lands in the focused pane or in the chrome (tab
    bar, sidebar, menus, popups): the operator is present at the client
    either way and the draft in the focused pane is still theirs. Scroll
    counts. Host colour replies and focus events do not.
17. **Preference** `focus_hold_seconds` under `[ui]` in
    `~/.gobby/client/prefs.toml`, `u64`, default 300. `0` disables expiry:
    focus then ends only when another pane is focused. No settings-popup row:
    the popup has no numeric step row and Josh asked for a configurable
    default, not a control.
18. **Dropped: Telegram `pane_focused` feedback.** The research report's task
    5 extended a queued-message notice that `9f76e9f6e2` ("silence queued
    Telegram wake notices") removed; `_deliver_inbound` now logs wake
    outcomes at debug. Nothing to extend.
19. **Order:** 1.1 lands before 1.2 (the gate reads `focus_held`) and before
    2.1 (the daemon must accept `terminal_focus` before gclient sends it).
20. **The golden corpus entry ships in 2.1.**
    `crates/gclient/src/daemon/ws.rs::GOLDEN_NAMES` is production gclient
    code that pins the manifest's names in order, and the Python and gclient
    corpus tests assert the count (43). A `focus.json` added by the daemon
    leaf would leave gclient and gterminals red until a crate change landed,
    and a gclient change needs the 0.0.1 bump. 2.1 already bumps, so the
    fixture, the manifest entry, the count updates and `GOLDEN_NAMES` land
    there. The protocol row stays in 1.1.
21. **Landing order.** Promote gclient 0.1.67 only after the P1 daemon
    restart: against a pre-1.1 daemon every `terminal_focus` report draws an
    `Unknown message type` warning and an error frame (`server.py` ~636-637).

Josh's ruling on web viewers (task #24784, relayed by the Orchestrator
gobby#17281, 2026-10-21):

22. "Why wouldn't a web viewer count as focused? If the user is on the web ui
    and typing, we should protect that too right? The goal is to prevent the
    daemon from injecting into the composer when the user has focus and is
    typing, and release that restriction when they're done." And, to a
    web-terminal leaf sending the same `terminal_focus` report with the same
    idle timer: "Yes, send it." Decision 9 no longer excludes web viewers,
    and 3.1 is that leaf. Rulings 1-6 and decisions 7-21 stand unchanged.

Planner decisions for the web terminal:

23. **Web focus rides the web lease.** The web terminal already takes the
    lease when its pane gains DOM focus (`TerminalTab.tsx`
    `takeControlOnFocus`) and releases it when focus leaves the pane (the
    pane's `onBlur` calls `releaseControl`). Focusing another pane, the chat,
    or any other control in the page therefore ends focus through the
    release path that 1.1 already clears, as a gclient focus move does. The
    report rides on that lease: `terminal_focus {focused: true}` once this
    attachment holds the lease, and `focused: false` on idle expiry while
    the lease is kept.
24. **A window switch does not end web focus** (ruling 4). The browser fires
    the pane's blur when the window loses focus, which today releases the
    lease. `useTmuxSessions`'s `releaseControl` returns without releasing
    when `document.hasFocus()` is false, so switching to another application
    keeps the lease and the focus. On return the browser refocuses the pane,
    and its repeated take is the holder's retry, which the registry grants
    (`leases.py` ~463). A click elsewhere inside the page still releases,
    because the document keeps focus. Consequence: a web pane left focused
    in a background window keeps the lease, so a gclient pane focused on the
    same terminal observes until its first keystroke takes over
    (`send_live_input`, `takeover: true`), as it already does against any
    peer holder.
25. **Web activity** is any `keydown`, `paste`, `pointerdown` or `wheel`
    event in the document while this attachment holds the lease, captured by
    document-level listeners that the focus report installs. Pointer motion
    does not count, as in decision 16. The page has one focused element, and
    while the pane holds the lease the pane has focus or the window is in
    the background, so these events are the operator at the pane; a click
    elsewhere in the page releases the lease anyway. Renderer protocol
    replies never count: the renderer's `onData` carries keystrokes and
    terminal replies alike, so the report watches DOM input, never the write
    path.
26. **Web preference** `focusHoldSeconds` lives in the web UI's client
    settings: `Settings` in `web/src/hooks/useSettings.ts`, default 300 in
    `DEFAULT_SETTINGS`, persisted per browser in the `gobby-settings`
    localStorage entry like `density`, and kept out of `PERSISTABLE_KEYS`
    because the daemon's `ui_settings` API has no such field. Settings,
    Appearance, edits it as a seconds field; `0` disables expiry. It lives in
    the browser for the reason gclient's lives in `prefs.toml`: the idle
    clock belongs to the client (ruling 5). The focus report reads it through
    a new `readFocusHoldSeconds()` each time it arms its timer, so a change
    applies from the next activity without threading settings through
    `TerminalTab.tsx`.
27. **Order:** 3.1 depends on 1.1 and, like 2.1, reaches users only after the
    P1 restart (decision 21). It needs no golden-corpus edit: 2.1 owns the
    corpus entry, and the web builds its frame in `tmuxSessionMessages.ts`
    like every other verb.

## As-Is Facts
`kind: framing`

Line numbers are navigation hints at HEAD `9f76e9f6e2`.

- gclient's input lease already follows in-app pane focus. `focus_live_pane`
  (`crates/gclient/src/app/live_loop/control.rs` ~37-45) calls
  `move_live_focus` (~56-65), which releases the previously focused pane
  (`release_live_control`, ~191-212, sends `terminal_release_control` through
  the pane writer barrier, `enqueue_release` ~241-265) and then requests
  control of the new pane unless a peer holds it (`Workspace::request_control`,
  `crates/gclient/src/app/live_control.rs` ~33-45; the take goes out in
  `start_control_request`, ~63-135, via `daemon.send_marking_written`).
- Typing into a live pane that is not writable queues the bytes and requests
  control with `takeover: true` (`send_live_input`, control.rs ~306-367).
- `LiveDaemon::send_ws(message: Value)` (`crates/gclient/src/daemon/mod.rs`
  ~665) sends one JSON message on the daemon websocket without waiting for a
  reply. `offer_focus_hints` (`live_loop/focus_hints.rs` ~64-88) is the
  pattern for a small per-loop state struct (`FocusMemo`) driven from
  `run_live_loop`.
- `run_live_loop` (`crates/gclient/src/app/live_loop.rs` ~134-659) selects on
  input, daemon frames, signals and a 16 ms `render_tick` (`RENDER_TICK`,
  `app/run_loop.rs` ~34). `route_live_input` (~677-766) routes every
  `RawInputEvent`; `chrome.focused_pane()` names the focused pane and
  `chrome.prefs` carries `ClientPrefs`.
- Preferences: `ClientPrefs` (`crates/gclient/src/ui/settings.rs` ~149-192,
  `deny_unknown_fields`) with `ClientPrefs::default` (~195-219); the file
  shape `UiPrefs` (`crates/gclient/src/prefs.rs` ~57-86, `deny_unknown_fields`)
  with `UiPrefs::from(&ClientPrefs)` (~95-117) and `ClientPrefs::from(PrefsFile)`
  (~143-168). `nerd_glyphs` is the most recent file-only key and shows every
  carrier: both structs, both `From` impls, the default, and the user guide.
  `crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys`
  pins the round trip.
- Daemon lease registry: `_Attachment` (`src/gobby/terminals/leases.py`
  ~125-143: viewer, backend, geometry, `host_input_granted`, no focus, no
  timestamp), `_Lease` (~146-150: holder, generation, sizing owner),
  `HolderChange` (~153-160) and `HolderObserver` (~163). `take_control`
  (~449-485), `release_control` (~487-508) and `finalize` (~510-575) all call
  `_notify_holder` (~438-447) under `lock(terminal_id)`. `holder_info`
  (~415-424) already exposes the holder's `viewer`. The single observer is
  `follow_lease_holder` in `src/gobby/runner_init/terminal_wiring.py`
  (~121-132), which syncs the gterm input grant; `runner.wake_dispatcher` is
  already in scope there (`set_terminal_manager`, ~141). When
  `front_door.routes.terminal_ws == "native"`, `runner.lease_registry` is
  `None` (~65-85).
- Websocket verbs: `TerminalControlMixin._handle_terminal_release_control`
  (`src/gobby/servers/websocket/terminal_ws_control.py` ~69-85) reads
  `attachment_id`, calls the registry, replies `terminal_control_result`.
  `server.py` registers handlers in the type map (~584) and lists off-loop
  types in `_OFF_LOOP_MESSAGE_TYPES` (~66-82). Golden messages live in
  `tests/fixtures/terminal_ws_golden/` with `manifest.json`, each stored as
  canonical compact JSON (sorted keys, no spaces) plus one newline.
  `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`
  (~290-303) round-trips every entry and asserts 43 names and 44 files
  (~292, ~296). `crates/gclient/src/daemon/ws.rs::GOLDEN_NAMES` (~9-53,
  production, re-exported from `lib.rs` ~20 and `daemon/mod.rs` ~26) pins the
  same list in order, and
  `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`
  (~22-64) asserts the count (~38) and list equality (~39).
  `crates/gterminals/tests/ws_golden.rs::canonical_terminal_ws_corpus_replays_byte_identically`
  (~54-97) and gdaemon's `ws_golden_proxy.rs`, `native_ws_transport.rs`
  (~607) and `nodes.rs` (~273) are manifest-driven with no count.
  `docs/contracts/gterm-protocols.md` has the
  message table (~213-233). gclient's test mock answers known verbs in
  `websocket_reply` (`crates/gclient/tests/mock_daemon/mod.rs` ~1144-1304) and
  returns `None` for anything else, which suits a reply-less verb.
- Wake dispatch: `WakeDispatcher._composer_blocks_wake`
  (`src/gobby/events/wake.py` ~711-767) is the single gate for both
  `_send_managed_terminal_wake` (~769-863, under `composer_action_lock`) and
  the batch path `wake_batch.py::dispatch_live_wakes` (~22-141). After the
  probe it checks `turn_in_flight_fingerprint`, then (#24768) returns
  "deliver, not confirmed empty" when any undelivered wake message is an
  operator comms wake (`src/gobby/events/comms_wake.py::is_operator_comms_wake`),
  forcing `clear_before_submit=True`. Withheld results in
  `RETRYABLE_WAKE_SKIPS` (`src/gobby/events/live_wake.py` ~32) are retried by
  `_schedule_wake_followup` (~309-328) and `_retry_withheld_wake` (~362-399,
  15 s doubling to 240 s). `dispatch_live_wake` (~273-292) takes the session's
  live-wake lock and schedules the followup. `set_terminal_manager` (~215-217)
  is the pattern for a late-bound service. `_dispatch_web_chat_wake` and
  `_web_chat_no_live_result` (~865-918) have no consumer outside `wake.py`.
- Web terminal (read at HEAD `ac3182b54f` for #24784):
  `useTmuxSessions` (`web/src/hooks/useTmuxSessions.ts`, 830 lines) owns the
  terminal websocket and the lease through `createControlLease`
  (`web/src/hooks/terminalControlLease.ts` ~96-401), whose snapshot carries
  `holder`; `hasControl` is `lease.holder === streamingId` (~712), and
  `releaseControl` (~685) calls `control.release()`. `TerminalTab.tsx` (986
  lines) wires `TerminalView`'s `onFocus` to `takeControlOnFocus` (~592-595)
  and `onBlur` to `keyboard.close()` then `releaseControl()` (~845-848).
  `TerminalView.tsx` (936 lines) raises both from its wrapper and ignores
  focus moves inside the pane (`stayedInside`). Frame builders live in
  `web/src/hooks/tmuxSessionMessages.ts` (`terminalReleaseControlMessage`
  ~154-163). The lease registry grants a repeated take by the holder as a
  retry (`leases.py` ~463). Client settings: `Settings`, `DEFAULT_SETTINGS`,
  `normalizePersistedSettings` and `useSettings` in
  `web/src/hooks/useSettings.ts` (546 lines); client-only keys such as
  `density` persist to the `gobby-settings` localStorage entry and stay out
  of `PERSISTABLE_KEYS`, and
  `web/src/components/settings/sections/AppearanceSection.tsx::AppearanceControls`
  renders them. Typed `Settings` and `UseSettingsReturn` fixtures live in
  `web/src/__tests__/settingsSliderFocus.test.ts` and
  `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx`.
  The daemon serves `web/dist` from disk (`src/gobby/servers/_app_ui.py`
  ~292-331), rebuilt by `uv run gobby ui build`.
- Telegram: `TelegramActionController._deliver_inbound`
  (`src/gobby/communications/telegram_actions.py` ~135-184) logs wake outcomes
  at debug since `9f76e9f6e2`; no queued-message notice remains.
- Sizes: `wake.py` 979 lines, `server.py` 863 lines (both over the 850 lint
  trigger), `leases.py` 796, `live_loop.rs` 795, `terminal_ws_control.py` 85.
- gclient version `0.1.66` in `crates/gclient/Cargo.toml`, `Cargo.lock` and
  `src/gobby/install/version_pins.py`.

## Constraints
`kind: framing`

- Planning only: this task edits nothing outside `.gobby/plans/`.
- `wake.py` and `server.py` are over 850 lines. 1.2 moves the web-chat wake
  path out of `wake.py`; 1.1 moves the message-class sets and their
  predicates out of `server.py`. Both moves are named in the owning
  deliverable with a new bare-path Target.
- No dispatch, lock acquisition or database read under the lease lock.
- `_composer_retry_wait(delay)` keeps its signature (replaced by name in
  `tests/events/test_wake.py`, `test_wake_composer_attention.py`,
  `test_wake_stale_active.py`, `test_wake_unread_dedup.py` and
  `tests/terminals/test_composer_proof_races.py`).
- Under the gdaemon front door (`terminal_ws == "native"`) the Python daemon
  has no lease registry; `set_lease_registry` is not called and the gate is
  inert (no hold). The gdaemon port of the terminal family (#21565) picks the
  verb up from the golden corpus.
- Tests run as
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <path>`;
  Rust as `CARGO_BUILD_JOBS=3 cargo test -p gobby-client --test <name>`;
  web from `web/` as `npx vitest run <files>`, `npm run type-check` and
  `npm run lint:js`.
- `TerminalTab.tsx` (986 lines) and `TerminalView.tsx` (936 lines) are over
  the 850 trigger; 3.1 edits neither, and keeps `useTmuxSessions.ts` under
  850 by putting its logic in a new module.

## P1: Daemon focus state and wake hold
`kind: framing`

Daemon-side work. Lands with a daemon restart (announce with a `global`
`send_message` before and after).

### 1.1 Lease registry focus state and the terminal_focus verb [category: code]
`kind: deliverable`

Targets:
- `src/gobby/terminals/leases.py::*` — scope-reason: add `_Attachment.focused`, `TerminalLeaseRegistry.set_focus`, `focus_held`, `set_focus_observer`, clear focus on release and finalize, fire the focus observer from `_notify_holder`
- `src/gobby/servers/websocket/terminal_ws_control.py::*` — scope-reason: add `_handle_terminal_focus`
- `src/gobby/servers/websocket/server.py::*` — scope-reason: register `terminal_focus` in the handler map; move the message-class sets and predicates out
- `src/gobby/servers/websocket/message_classes.py`
- `docs/contracts/gterm-protocols.md`
- `tests/terminals/test_lease_authority.py::*` — scope-reason: focus state tests
- `tests/servers/test_terminal_ws_lease.py::*` — scope-reason: verb test

**Research context:** `_Attachment` (`leases.py` ~125-143) gains
`focused: bool = False`. `TerminalLeaseRegistry` gains:

- `async set_focus(attachment_id, focused) -> bool`: under
  `lock(record.terminal_id)`, set `record.focused` on a live record; return
  `False` for a stale or finalized attachment. When `focused` is `False` and
  the record is the terminal's holder, call the focus observer.
- `focus_held(terminal_id) -> bool`: the holder record exists, is not
  finalized, and has `focused`, whatever its viewer (decision 9). Pure read,
  no lock
  (same discipline as `holder_info`, ~415-424).
- `set_focus_observer(observer: Callable[[str], None] | None)`: one slot,
  mirroring `set_holder_observer` (~429-436). The observer is synchronous;
  `_notify_holder` (~438-447) calls it with the terminal id on every holder
  change, before its early return when no holder observer is installed
  (~445-446) and before awaiting the holder observer. It runs under the lease
  lock,
  so it must only schedule work (1.2's `release_focus_hold` pops a dict and
  creates tasks). `_notify_holder` gains a keyword `holder_changed: bool =
  True`; `take_control`'s repeated-take retry branch (~463-469), where the
  holder does not change, passes `False` and the focus observer is skipped,
  so a web refocus after a window switch (decision 24) does not re-dispatch
  a held wake.
- `release_control` (~487-508) and `finalize` (~510-575) set
  `record.focused = False` on the record they release, and `take_control`
  (~449-485) clears it on the displaced holder's record when `takeover`
  replaces the holder (`self.get(displaced)`, one line beside ~472-474), so a
  gclient report is never older than the lease it describes. `take_control`
  leaves the new holder's `focused` alone: gclient reports focus after the
  grant (2.1).

Websocket verb: `TerminalControlMixin._handle_terminal_focus(websocket, data)`
in `terminal_ws_control.py` reads `attachment_id` and `focused`
(`bool(data.get("focused"))`), calls `self._leases().set_focus(...)`, and
sends nothing; a stale attachment logs at debug. It follows
`_handle_terminal_release_control` (~69-85), which also trusts the
attachment id without a socket-ownership check. Register it in `server.py`'s
type map next to `terminal_release_control` (~584) and add `"terminal_focus"`
to `_OFF_LOOP_MESSAGE_TYPES` so it stays in per-terminal order with take and
release.

Move, for the size lint: `server.py` is 863 lines. Move the module-level
message-class sets `_OFF_LOOP_MESSAGE_TYPES`, `_WORKSPACE_CHAIN_TYPES`,
`_READ_WORKSPACE_OPS`, `_DURABLE_WORKSPACE_OPS` and `_DURABLE_TERMINAL_TYPES`
(~60-90) together with the three predicates that read them, the off-loop
predicate (~94-103), `_survives_disconnect` (~106-119) and the workspace chain
classifier (~125-135), into the new `src/gobby/servers/websocket/message_classes.py`;
`server.py` imports the three predicates. `SLOW_WEBSOCKET_HANDLER_SECONDS`
stays in `server.py` because `tests/servers/websocket/test_server.py` imports
and monkeypatches it there (~19, ~213). No other module reads the moved names
(`gcode grep -E "_OFF_LOOP_MESSAGE_TYPES|_WORKSPACE_CHAIN_TYPES|_DURABLE_TERMINAL_TYPES|_READ_WORKSPACE_OPS|_DURABLE_WORKSPACE_OPS" src/ tests/`
hits only `server.py`).

Golden corpus: the `focus.json` fixture, its manifest entry, `GOLDEN_NAMES`
and the count updates ship in 2.1 (decision 20). Document the verb as a new
row in the `docs/contracts/gterm-protocols.md` message table: client to
daemon, `terminal_id`, `attachment_id`, `focused`; no reply; a viewer state
report, sent by gclient (2.1) and the web terminal (3.1), that the daemon
clears on release, finalize, and takeover.

Observed checks: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/terminals/test_lease_authority.py tests/servers/test_terminal_ws_lease.py tests/servers/test_terminal_ws_golden.py tests/events/test_comms_wake.py tests/events/test_wake_native_terminal.py tests/terminals/test_composition_roots.py -q`
ran at HEAD `b2190c8f2f` during planning as a baseline: 103 passed in 30 s.
Nothing in this plan is implemented yet. Planned: the first three files
again, plus `uv run ruff check src/` and `uv run mypy src/`.

Activation: daemon code; live after a daemon restart, announced with a
`global` `send_message` before and after.

**Acceptance:**

- 1.1.1 - `_Attachment` carries `focused`, default `False`; `focus_held` is true only for a live holder with `focused` set, whether its viewer is `gclient` or `web`, and false for an unfocused holder and for a focused non-holder. test: `tests/terminals/test_lease_authority.py::test_focus_held_requires_a_focused_holder_of_either_viewer`.
- 1.1.2 - `release_control`, `finalize` and a `takeover` in `take_control` clear the released or displaced record's `focused`; a stale attachment makes `set_focus` return `False`. test: `tests/terminals/test_lease_authority.py::test_release_finalize_and_takeover_clear_focus`.
- 1.1.3 - The focus observer fires synchronously with the terminal id on `set_focus(False)` of the holder and on every holder change from `_notify_holder`, with or without a holder observer installed, never on the holder's repeated-take retry, and is never awaited. test: `tests/terminals/test_lease_authority.py::test_focus_observer_fires_on_focus_loss_and_holder_change`.
- 1.1.4 - `terminal_focus` reaches `set_focus` through the websocket type map, sends no reply, and is an off-loop message type. test: `tests/servers/test_terminal_ws_lease.py::test_terminal_focus_marks_the_holder_and_sends_nothing`.
- 1.1.5 - `message_classes.py` holds the message-class sets and predicates and `server.py` is under 850 lines. file: `src/gobby/servers/websocket/message_classes.py`.
- 1.1.6 - The protocol contract documents `terminal_focus`. behavior: "terminal_focus" row in `docs/contracts/gterm-protocols.md`.

### 1.2 Operator comms wakes hold on a focused pane and dispatch on release [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/events/live_wake.py::*` — scope-reason: add `PANE_FOCUSED`, `pane_focused_result`, extend `RETRYABLE_WAKE_SKIPS`
- `src/gobby/events/wake.py::*` — scope-reason: `set_lease_registry`, the `pane_focused` gate with `priority`, `release_focus_hold`; move the web-chat wake path out
- `src/gobby/events/wake_web_chat.py`
- `src/gobby/events/wake_batch.py::dispatch_live_wakes`
- `src/gobby/runner_init/terminal_wiring.py::init_terminal_wiring`
- `docs/guides/sessions.md`
- `docs/guides/agents.md`
- `tests/events/test_comms_wake.py::*` — scope-reason: hold, release, ordering and scope tests
- `tests/events/test_wake_native_terminal.py::*` — scope-reason: batch path hold test
- `tests/terminals/test_composition_roots.py::*` — scope-reason: wiring assertions

Consumers unchanged:
- `tests/e2e/composer_proof_races.py` — no-edit-reason: builds its skip set from `RETRYABLE_WAKE_SKIPS` and accepts a new member.
- `tests/e2e/composer_proof_trace.py` — no-edit-reason: unions `RETRYABLE_WAKE_SKIPS` into its accepted outcomes and accepts a new member.
- `src/gobby/runner_init/servers.py` — no-edit-reason: calls `set_web_chat_session_registry`, which stays on `WakeDispatcher`.
- `tests/events/test_wake_unread_dedup.py` — no-edit-reason: calls `dispatch_live_wakes([recipient])` through its public signature, which does not change; `priority` is read inside the batch path.
- `src/gobby/runner_init/orchestration.py` — no-edit-reason: calls `init_terminal_wiring(runner, config)`; the signature does not change and the new bindings happen inside it.
- `tests/terminals/test_host_shutdown_preservation.py` — no-edit-reason: drives `init_terminal_wiring` with a `MagicMock` runner, which accepts the two new dispatcher and registry calls without edits.

**Research context:** `live_wake.py` (~27-32) gains
`PANE_FOCUSED = "pane_focused"`, adds it to `RETRYABLE_WAKE_SKIPS`, and a
`pane_focused_result(session_id, *, method)` shaped like
`composer_occupied_result` (~118-132) with `skipped: "pane_focused"` and
`reason: "terminal pane focused; delivered when focus ends"`.

`WakeDispatcher` (`wake.py`) gains, following `set_terminal_manager`
(~215-217): `set_lease_registry(registry)` storing `self._lease_registry`
(default `None`, so every existing test construction is unaffected and the
front-door daemon stays inert), and
`self._focus_holds: dict[str, dict[str, str]]` (terminal id to session id to
priority).

Gate: `_composer_blocks_wake` (~711-767) gains a keyword `priority: str =
"normal"`, passed by `_send_managed_terminal_wake` (~795) and
`wake_batch.py::dispatch_live_wakes` (~118). After the
`turn_in_flight_fingerprint` check and the `pending` read, before the
#24768 comms branch:

```python
comms_pending = any(is_operator_comms_wake(message) for message in pending)
terminal_id = None if terminal is None else str(terminal.id)
if comms_pending and terminal_id is not None and self._lease_registry is not None:
    if self._lease_registry.focus_held(terminal_id):
        self._focus_holds.setdefault(terminal_id, {})[session_id] = priority
        return pane_focused_result(session_id, method=method), False
    self._focus_holds.get(terminal_id, {}).pop(session_id, None)
if comms_pending:
    return None, False  # existing #24768 branch, unchanged
```

The `active` session row check (`_dispatch_live_wake_unlocked` ~542-561 and
`wake_batch.py` ~77) and the open-turn check still run first, so a wake never
steers a running turn (ruling 6). Ordinary agent wakes never enter the hold.

Release: `release_focus_hold(terminal_id)` is synchronous. It pops
`self._focus_holds[terminal_id]` and, for each session, creates a task on the
owner loop (`_require_owner_loop`, ~203-206) running
`self.dispatch_live_wake(session_id, priority=priority, require_unread=True)`,
tracked and forgotten like `_schedule_deferred_refresh` (~330-341).
`dispatch_live_wake` (~273-292) gains `require_unread: bool = False` and
passes it to `_dispatch_live_wake_unlocked` (~507), which already returns
`skipped: "mailbox_read"` when the session has no undelivered message
(~512-520); the backoff loop passes the same flag (~372-374). A DM the
operator's own turn read through hook context while the hold stood is
therefore never replayed with a wipe. `dispatch_live_wake` takes the
session's live-wake lock, so the attempt serializes against a backoff retry
in flight; a result still withheld schedules its own followup.
`_retry_withheld_wake` (~362-399) has no attempt cap: it
loops until a non-retryable outcome, so with `pane_focused` retryable the
15 s to 240 s backoff keeps polling as the fallback when a release event is
lost, and the 240 s ceiling is a poll interval, not a deadline.

Wiring: in `terminal_wiring.py::init_terminal_wiring`, after
`runner.wake_dispatcher.set_terminal_manager(...)` (~141), call
`runner.wake_dispatcher.set_lease_registry(runner.lease_registry)` and
`runner.lease_registry.set_focus_observer(runner.wake_dispatcher.release_focus_hold)`.
The lease registry fires the observer on `set_focus(False)` and on every
holder change (1.1), so a focus report, a release, a takeover, a socket loss
and a gclient crash all release the hold through one path; the observer only
pops a dict and creates tasks, so nothing runs under the lease lock. The
front-door branch (~65-85) returns before this point and wires nothing.

Move, for the size lint: `wake.py` is 979 lines. Move `_dispatch_web_chat_wake`
and `_web_chat_no_live_result` (~865-918) into the new
`src/gobby/events/wake_web_chat.py` as `dispatch_web_chat_wake(dispatcher, session_id, *, priority)`,
the pattern `wake_batch.py` already uses for a sibling that reads dispatcher
internals; `_dispatch_live_wake_unlocked` (~576) calls it. Arithmetic: 979
lines, minus the 55-line web-chat block, plus roughly 40 lines for the
registry setter, the hold map, the gate, the `require_unread` passthrough and
`release_focus_hold`, lands near 965; the executor budgets within that
35-line margin to the 1,000-line ceiling. Only `wake.py`
calls either symbol (`gcode grep -w _dispatch_web_chat_wake src/ tests/` and
`gcode grep -w _web_chat_no_live_result src/ tests/` hit `wake.py` alone), and
`set_web_chat_session_registry` stays on the dispatcher for
`runner_init/servers.py` (~95) and `tests/test_runner_lifecycle.py` (~503).

Docs: `docs/guides/sessions.md` (~599) and `docs/guides/agents.md` (~546)
name `composer_occupied` as the withheld reason for an operator draft. Add
`pane_focused` beside it: an operator comms wake to a seat whose pane is
held focused in gclient or the web terminal is withheld and delivered, with
the composer cleared, when focus ends by idle timeout or by focusing another
pane.

Test shape: `tests/events/test_comms_wake.py::test_operator_comms_wake_replaces_idle_composer_only`
(~167-220) builds a seat with a native terminal row, a `ComposerLedger`
draft and a system-originated comms message; new tests reuse `_seat` and
set a `TerminalLeaseRegistry` on the dispatcher with an attached `gclient`
record that holds the lease and has `focused=True`; the 1.2.4 test adds a
focused `web` record, which holds like gclient. The release test replaces
`_composer_retry_wait` with a parked wait (as
`test_wake_composer_attention.py` ~112 does) so only the release path can
deliver, then calls `release_focus_hold` and awaits the created task. The
already-read test marks the comms message delivered before the release and
asserts `mailbox_read` with no terminal write and no ledger clear.

Observed checks: the baseline run recorded in 1.1 covered
`tests/events/test_comms_wake.py`, `tests/events/test_wake_native_terminal.py`
and `tests/terminals/test_composition_roots.py` (103 passed at `b2190c8f2f`,
nothing implemented yet). Planned: those three files plus
`tests/events/test_wake.py`, `uv run ruff check src/` and `uv run mypy src/`.

**Granularity:** six hand-maintained production files (`live_wake.py`,
`wake.py`, `wake_web_chat.py`, `wake_batch.py`, `terminal_wiring.py` and the
two guides are docs), one lifecycle owner (the dispatcher's hold map). The
web-chat move is the size-lint split for the file the gate edits and ships in
the same commit.

Activation: daemon code; live after a daemon restart, announced with a
`global` `send_message` before and after. 1.1 and 1.2 can share one restart.

**Acceptance:**

- 1.2.1 - With a focused holder and a pending operator comms wake, `dispatch_live_wake` returns `skipped: "pane_focused"`, writes nothing to the terminal, records the hold, and the result is retryable. test: `tests/events/test_comms_wake.py::test_operator_comms_wake_holds_while_the_pane_is_focused`.
- 1.2.2 - `release_focus_hold` re-dispatches the held session once and the wake lands with `clear_before_submit=True`; a second release is a no-op. test: `tests/events/test_comms_wake.py::test_focus_release_dispatches_the_held_wake_with_a_wipe`.
- 1.2.3 - An open transcript turn withholds as `composer_unconfirmed` before the focus hold is consulted, and an `active` row withholds as `session_active`. test: `tests/events/test_comms_wake.py::test_active_turn_protection_outranks_the_focus_hold`.
- 1.2.4 - A focused `web` holder holds like a focused `gclient` holder, while an unfocused holder of either viewer, a registry-less dispatcher, and a pending wake that is not an operator comms wake all take today's path. test: `tests/events/test_comms_wake.py::test_focus_hold_applies_only_to_comms_wakes_with_a_focused_holder`.
- 1.2.5 - The batch path returns `pane_focused` for a focused recipient and still sends to the others in one host exchange. test: `tests/events/test_wake_native_terminal.py::test_batch_wake_holds_a_focused_recipient_and_sends_the_rest`.
- 1.2.6 - `init_terminal_wiring` binds the lease registry to the dispatcher and the dispatcher's `release_focus_hold` as the focus observer; the front-door branch binds neither. test: `tests/terminals/test_composition_roots.py::test_terminal_wiring_binds_focus_release_to_the_wake_dispatcher`.
- 1.2.7 - `wake_web_chat.py` carries the web-chat wake path and `wake.py` is under 1,000 lines. file: `src/gobby/events/wake_web_chat.py`.
- 1.2.8 - The guides name `pane_focused` beside `composer_occupied`. behavior: "pane_focused" in `docs/guides/sessions.md` and `docs/guides/agents.md`.
- 1.2.9 - `release_focus_hold` for a held session whose mailbox has no undelivered message returns `skipped: "mailbox_read"`, writes nothing to the terminal and clears no composer. test: `tests/events/test_comms_wake.py::test_focus_release_after_the_mailbox_was_read_sends_nothing`.

## P2: gclient idle focus report
`kind: framing`

Crate work. Lands with a gclient rebuild and install (`promote_workspace_binary_set`
promotes gclient on its own; the daemon needs no restart for it). Promote
only after P1's restart has landed 1.1 (decision 21).

### 2.1 gclient reports pane focus and its idle expiry [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/live_loop/focus_hold.rs`
- `crates/gclient/src/app/live_loop.rs::run_live_loop`
- `crates/gclient/src/ui/settings.rs::ClientPrefs`
- `crates/gclient/src/ui/settings.rs::ClientPrefs::default`
- `crates/gclient/src/prefs.rs::UiPrefs`
- `crates/gclient/src/prefs.rs::UiPrefs::from`
- `crates/gclient/src/prefs.rs::ClientPrefs::from`
- `crates/gclient/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: pin gclient 0.1.67
- `crates/gclient/src/daemon/ws.rs::GOLDEN_NAMES`
- `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`
- `tests/fixtures/terminal_ws_golden/focus.json`
- `tests/fixtures/terminal_ws_golden/manifest.json::*` — scope-reason: list `focus.json` after `release_control.json`
- `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`
- `docs/guides/gclient-user-guide.md`
- `crates/gclient/tests/lease_control.rs::*` — scope-reason: idle expiry and refocus test
- `crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys`

Consumers unchanged:
- `crates/gclient/src/views/mod.rs` — no-edit-reason: calls `run_live_loop(&mut workspace, &mut terminal, &mut chrome, input, switch)`; the signature does not change and `FocusHold` lives inside the loop.
- `crates/gclient/src/ui/chrome.rs` — no-edit-reason: fills `prefs` with `ClientPrefs::default()`, which supplies the new field.
- `crates/gclient/src/ui/pane_layout.rs` — no-edit-reason: builds test prefs with `ClientPrefs::default()` and `..ClientPrefs::default()`, which supply the new field.
- `crates/gclient/src/app/mod.rs` — no-edit-reason: re-exports `run_live_loop`; the name and signature do not change.
- `crates/gclient/src/lib.rs` — no-edit-reason: re-exports `GOLDEN_NAMES`; the constant's name and type do not change.
- `crates/gclient/src/daemon/mod.rs` — no-edit-reason: re-exports `GOLDEN_NAMES`; the constant's name and type do not change.
- `crates/gterminals/tests/ws_golden.rs` — no-edit-reason: manifest-driven with no count assertion; it replays the new fixture byte-for-byte unchanged.
- `crates/gdaemon/tests/ws_golden_proxy.rs` — no-edit-reason: manifest-driven generic replay with no count assertion.
- `crates/gdaemon/tests/native_ws_transport.rs` — no-edit-reason: manifest-driven generic replay with no count assertion.
- `crates/gdaemon/tests/nodes.rs` — no-edit-reason: manifest-driven generic replay with no count assertion.

**Research context:** gclient already takes the lease on pane focus and
releases it on focus move (`focus_live_pane`, `move_live_focus`,
`release_live_control` in `live_loop/control.rs`), so the daemon learns of
"focused another pane" today. What is new is the report that the holder is
focused, and its idle expiry.

New module `crates/gclient/src/app/live_loop/focus_hold.rs`, declared in
`live_loop.rs` beside `mod focus_hints;` (~41), with
`pub(super) struct FocusHold { reported: Option<(PaneId, String)>, last_activity: Instant }`:

- `new(now)`.
- `note_activity(now)`: `last_activity = now`.
- `sync(workspace, chrome, now, hold_seconds)`: returns without sending when
  `workspace.exit_reason().is_some()` or `!workspace.daemon_ready()`. Let
  `key` be `(pane, attachment_id)` for `chrome.focused_pane()` when that pane
  `is_live()` and `is_held()`, else `None`. Let `idle` be
  `hold_seconds > 0 && now - last_activity >= hold_seconds`. If `reported` is
  `Some` and (`idle` or `reported != key`): when `idle` and the reported pane
  is still live, send `terminal_focus {focused: false}` for it; set
  `reported = None`. Then, if `!idle` and `key` is `Some` and
  `reported != key`: send `terminal_focus {focused: true}` for it and set
  `reported = key`. Messages go through `workspace.daemon().send_ws(json!({...}))`
  (`daemon/mod.rs` ~665); a send error is ignored like a lost focus hint,
  because the next tick re-reports.

Because `sync` runs on every render tick, a granted take (the pane becomes
`is_held()`) reports focus within one tick, which covers first focus,
daemon reconnect (the re-taken lease on the new attachment id changes `key`)
and typing after an idle expiry while the lease was kept. A focus move needs
no false report: the release already cleared the daemon's flag (1.1), so
`sync` just forgets the old key. `hold_seconds == 0` never sets `idle`.

`run_live_loop` (~134-659): own `let mut focus_hold = FocusHold::new(Instant::now())`
beside `FocusMemo`; in the input arm, call `focus_hold.note_activity(now)`
for every `RawInputEvent` that is a key, text or paste event, or a mouse event
whose kind is not `MouseEventKind::Moved` (decision 16), before
`route_live_input`; in the `render_tick` arm (~511), call
`focus_hold.sync(workspace, chrome, Instant::now(), chrome.prefs.focus_hold_seconds)`.
`live_loop.rs` is 795 lines; the loop gains about six lines and the logic
lives in the new module.

Preference `focus_hold_seconds: u64` (default 300, decision 17): add to
`ClientPrefs` (`settings.rs` ~149-192) with a doc comment, to
`ClientPrefs::default` (~195-219), to `UiPrefs` (`prefs.rs` ~57-86, before
`project_order` so the TOML sub-table stays last), and to both `From` impls
(~95-117, ~143-168). Extend
`crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys`
(~612) to set and assert `focus_hold_seconds`. Document it in
`docs/guides/gclient-user-guide.md`: a sentence under **Settings** after the
`nerd_glyphs` paragraph pattern (~312) and a `focus_hold_seconds = 300` line
in the `[ui]` example (~787-800), and a paragraph under **Attach and control**
(~606) stating that a focused pane holds operator messages addressed to its
seat until `focus_hold_seconds` pass without activity in it or another pane
is focused, after which the daemon clears the composer and delivers; `0`
disables the timeout. Keep the guide's `docs/guides/README.md` row coherent
(memory `392cc53f`).

Test shape: `crates/gclient/tests/lease_control.rs::focus_claims_a_free_pane_in_one_gesture`
(~92-140) drives `run_live_loop` against `MockDaemon` with
`wait_for_websocket_requests(&mock, kind, n)` and `send_key`. The mock's
`websocket_reply` returns `None` for `terminal_focus`, which is the verb's
contract. The new test sets `chrome.prefs.focus_hold_seconds = 1`, waits for
`terminal_take_control`, then for one `terminal_focus` with `focused: true`,
then (no input for over a second) one with `focused: false`, then sends a key
and waits for `focused: true` again and `terminal_input`; it asserts the pane
stayed `is_held()` throughout and exactly one `terminal_take_control` was
sent. Unit tests in `focus_hold.rs` cover `0` disabling expiry and a focus
move forgetting the key without a false report.

Golden corpus (decision 20): add `tests/fixtures/terminal_ws_golden/focus.json`
holding exactly `canonical_json(message)` plus one newline, as every fixture
in the directory is stored (compact JSON, keys sorted, no spaces;
`take_control.json` is the model):

```
{"attachment_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","focused":true,"terminal_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","type":"terminal_focus"}
```

Insert `"focus.json"` after `"release_control.json"` in `manifest.json` and
at the same position in `crates/gclient/src/daemon/ws.rs::GOLDEN_NAMES`
(~9-53): `corpus_replays_from_canonical_manifest` asserts list equality, so
order matters. Update the counts in
`tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`
(~292: `43` to `44`; ~296: `44` to `45`) and in
`crates/gclient/tests/ws_golden.rs` (~38: `43` to `44`). The gterminals and
gdaemon corpus tests are manifest-driven with no count and need no edit;
they run as planned checks. Python's `decode_message`/`encode_message` and
gterminals' `decode_message` (`ws_protocol.rs` ~131) return generic JSON
values, so the new type round-trips without a decoder change.

Version: bump `crates/gclient/Cargo.toml` to `0.1.67`, refresh `Cargo.lock`,
and set `"gclient": "0.1.67"` in `src/gobby/install/version_pins.py`.

Observed checks: none run for this plan beyond the 1.1 baseline, which
included `tests/servers/test_terminal_ws_golden.py`. Planned:
`CARGO_BUILD_JOBS=3 cargo test -p gobby-client --test lease_control --test startup --test ws_golden`,
`cargo test -p gobby-client --lib app::live_loop::focus_hold`,
`cargo test -p gobby-terminals --test ws_golden`,
`cargo test -p gobby-daemon --test ws_golden_proxy --test native_ws_transport --test nodes`,
`uv run pytest tests/servers/test_terminal_ws_golden.py`,
`cargo clippy -p gobby-client`, and the gclient parity and screen suites
unchanged.

**Granularity:** six hand-maintained production files (`focus_hold.rs`,
`live_loop.rs`, `settings.rs`, `prefs.rs`, `ws.rs`, `version_pins.py`) plus
the crate manifests, the guide, one fixture, its manifest and two corpus
tests; one crate bump shipping two things that cannot be split without a
second bump: the focus report with its expiry, and the corpus entry that pins
the verb's wire shape for gclient, gterminals and gdaemon. One commit, one
test run.

Activation: gclient crate; live after `CARGO_BUILD_JOBS=3 cargo build --release -p gobby-client`
and promotion through `promote_workspace_binary_set`, which promotes gclient
on its own. No daemon restart, but promote only after the P1 restart
(decision 21); the fixture and Python test edits in this leaf need no
activation.

**Acceptance:**

- 2.1.1 - After the focused pane's take is granted, gclient sends one `terminal_focus {focused: true}` for that attachment and no second `terminal_take_control`. test: `crates/gclient/tests/lease_control.rs::idle_expiry_reports_focus_lost_and_activity_restores_it`.
- 2.1.2 - With `focus_hold_seconds = 1` and no activity, gclient sends `terminal_focus {focused: false}` for the held pane without releasing the lease; a keystroke then sends `focused: true` again and the key reaches `terminal_input`. test: `crates/gclient/tests/lease_control.rs::idle_expiry_reports_focus_lost_and_activity_restores_it`.
- 2.1.3 - `FocusHold::sync` never marks idle when `hold_seconds` is `0`, and a focused-pane change forgets the reported key without sending `focused: false`. test: `crates/gclient/src/app/live_loop/focus_hold.rs::zero_hold_never_expires_and_focus_moves_forget_the_key`.
- 2.1.4 - `focus_hold_seconds` round-trips through `prefs.toml` with default `300`, and an unknown key still fails to load. test: `crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys`.
- 2.1.5 - The user guide documents `focus_hold_seconds` and the focus hold behavior. behavior: "focus_hold_seconds" in `docs/guides/gclient-user-guide.md`.
- 2.1.6 - gclient ships as `0.1.67` in `Cargo.toml`, `Cargo.lock` and the managed version pin. file: `src/gobby/install/version_pins.py`.
- 2.1.7 - `focus.json` is listed in the golden manifest after `release_control.json`, `GOLDEN_NAMES` lists it at the same position, and the Python corpus test round-trips it with 44 names and 45 files. test: `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`.
- 2.1.8 - The gclient corpus test replays the 44-name manifest against `GOLDEN_NAMES`. test: `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`.

## P3: Web terminal idle focus report
`kind: framing`

Web UI work. Lands with `uv run gobby ui build` and a browser reload; no
daemon restart and no binary install. Ship only after P1's restart has
landed 1.1 (decision 27).

### 3.1 Web terminal reports pane focus and its idle expiry [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `web/src/hooks/terminalFocusReport.ts`
- `web/src/hooks/useTmuxSessions.ts::useTmuxSessions`
- `web/src/hooks/tmuxSessionMessages.ts::*` — scope-reason: add `terminalFocusMessage` beside `terminalReleaseControlMessage`
- `web/src/hooks/useSettings.ts::*` — scope-reason: add `focusHoldSeconds` to `Settings`, `DEFAULT_SETTINGS` and `normalizePersistedSettings`, add `updateFocusHoldSeconds` to `useSettings`, export `readFocusHoldSeconds`
- `web/src/components/settings/sections/AppearanceSection.tsx::*` — scope-reason: add the focus-hold field to `AppearanceControls`
- `docs/guides/web-ui.md`
- `web/src/hooks/__tests__/terminalFocusReport.test.ts`
- `web/src/hooks/__tests__/useTmuxSessions.test.ts::*` — scope-reason: report wiring and window-blur tests; stub `document.hasFocus` in the existing release tests
- `web/src/hooks/__tests__/useSettings.test.ts::*` — scope-reason: `focusHoldSeconds` default, normalization and localStorage persistence
- `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx::*` — scope-reason: the seconds field; the typed fixture gains the key and updater
- `web/src/__tests__/settingsSliderFocus.test.ts::*` — scope-reason: the typed `Settings` and `UseSettingsReturn` fixtures gain the key and updater

Consumers unchanged:
- `web/src/components/activity/terminal/TerminalTab.tsx` — no-edit-reason: calls `useTmuxSessions(projectId)` and wires `onFocus` and `onBlur` to `takeControl` and `releaseControl`; the hook's signature and result shape do not change, and the window check lives inside `releaseControl`.
- `web/src/components/activity/terminal/TerminalView.tsx` — no-edit-reason: raises `onFocus` and `onBlur` as today; activity is captured at the document.
- `web/src/hooks/terminalControlLease.ts` — no-edit-reason: the report reads the lease through the hook's snapshot, and `release()` is unchanged.
- `web/src/components/activity/terminal/__tests__/terminalTabFixtures.ts` — no-edit-reason: mocks the hook result, whose shape does not change.
- `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx` — no-edit-reason: mocks `useTmuxSessions`, so its blur assertion still sees `releaseControl` called once.
- `web/src/components/activity/__tests__/SessionsTab.test.tsx` — no-edit-reason: mocks the hook result, whose shape does not change.
- `web/src/components/activity/terminal/__tests__/TerminalTabViewport.test.tsx` — no-edit-reason: mocks the hook with its own `mockUseTmuxSessions`; the result shape does not change.
- `web/src/__tests__/App.test.tsx` — no-edit-reason: mocks `useSettings` with an untyped object and never reads the new key.
- `web/src/components/settings/sections/__tests__/ChatVoiceSection.test.tsx` — no-edit-reason: casts a partial fixture `as unknown as UseSettingsReturn`.
- `web/src/components/settings/sections/__tests__/ProvidersModelsSection.test.tsx` — no-edit-reason: casts a partial fixture `as unknown as UseSettingsReturn`.
- `src/gobby/config/registry.py` — no-edit-reason: `focusHoldSeconds` is client-only (decision 26), so no `ui_settings.*` key is added.

**Research context:** the web pane already takes the lease on DOM focus and
releases it when focus leaves the pane (decision 23), so the daemon learns of
"focused something else" today. What is new is the report that the holder is
focused, its idle expiry, and keeping the lease across a window switch
(decision 24). Copied facts: `useTmuxSessions` owns the socket (`wsRef`), the
attachment (`streamingIdRef`, `attachedTargetRef`) and the lease snapshot
(`lease`, set by `createControlLease`'s `onChange: setLease`, ~147-155);
`releaseControl` is `useCallback(() => control.release(), [control])`
(~685); `TerminalTab.tsx` is the hook's only production caller, and every
test that renders `TerminalTab` mocks the hook.

New module `web/src/hooks/terminalFocusReport.ts`:

```ts
export interface FocusReportDeps {
  socket: () => WebSocket | null;
  terminalId: () => string | undefined;
  holdSeconds: () => number;
}

export interface FocusReport {
  sync: (holder: string | null, attachmentId: string | null) => void;
  activity: () => void;
  listen: (target: Document) => () => void;
  dispose: () => void;
}

export function createFocusReport(deps: FocusReportDeps): FocusReport;
```

State: `held`, the attachment id this client holds the lease with, or
`null`; `reported`, the attachment id last reported focused, or `null`; one
timer.

- `sync(holder, attachmentId)` runs after every lease or attachment change.
  `held` becomes `holder` when `holder !== null && holder === attachmentId`,
  else `null`. When `held` is `null`, clear the timer and set
  `reported = null` without sending: release, takeover and finalize already
  cleared the daemon's flag (1.1). Otherwise, when `reported !== held`,
  report `focused: true` for `held`, set `reported = held`, and arm the
  timer; the grant follows the operator focusing the pane, so it counts as
  activity.
- `activity()`: nothing when `held` is `null`. Otherwise, when `reported` is
  `null` because the timer expired, report `focused: true` and set
  `reported = held`; then re-arm.
- Arming clears any timer, reads `deps.holdSeconds()`, and when it is above
  `0` sets `window.setTimeout(expire, seconds * 1000)`; `0` never arms.
- `expire` sends `focused: false` for `reported` and sets `reported = null`;
  `held` and the lease stay, so the next keystroke lands directly.
- A report is `terminalFocusMessage(terminalId, attachmentId, focused)` sent
  only when `deps.socket()` is `OPEN`; otherwise it is dropped. A dropped
  `true` is repaired by the reconnect's new attachment and lease, and a
  dropped `false` by the daemon finalizing the lost attachment, which clears
  `focused` (1.1).
- `listen(document)` adds capture-phase passive listeners for `keydown`,
  `paste`, `pointerdown` and `wheel` that call `activity`, and returns their
  remover (decision 25). `dispose()` clears the timer and both states.

A background tab's timers may be throttled to about once a minute, so an
expiry in a hidden tab can land up to a minute late; that errs toward
holding the wake, which ruling 4 accepts for a window switch.

`terminalFocusMessage(terminalId, attachmentId, focused)` in
`tmuxSessionMessages.ts` returns
`JSON.stringify({type: "terminal_focus", terminal_id, attachment_id, focused})`,
the same shape as the 2.1 golden fixture.

Hook wiring in `useTmuxSessions`, at most 15 added lines so the file stays
under 850:

- `const [focusReport] = useState(() => createFocusReport({socket: () => wsRef.current, terminalId: () => attachedTargetRef.current?.terminal_id, holdSeconds: readFocusHoldSeconds}))`
  beside `control`, inside the same eslint block.
- `useEffect(() => focusReport.sync(lease.holder, streamingId), [focusReport, lease.holder, streamingId])`.
- `useEffect(() => { const stop = focusReport.listen(document); return () => { stop(); focusReport.dispose(); }; }, [focusReport])`.
- `releaseControl` returns early when `!document.hasFocus()`, with a comment
  citing ruling 4 (decision 24), then calls `control.release()`. The unit
  test stubs `hasFocus`, so V2 step 12 is the real-browser proof: if a
  window switch still releases there, the executor defers the check with
  `window.setTimeout(..., 0)` so focus settles before deciding.

Preference (decision 26): `focusHoldSeconds: number` in `Settings`, `300` in
`DEFAULT_SETTINGS`; `normalizePersistedSettings` keeps a finite non-negative
number, floored to an integer, and drops anything else; `useSettings` gains
`updateFocusHoldSeconds(seconds)` calling `updateSettings({focusHoldSeconds})`,
which persists through the existing localStorage save, and the key stays out
of `PERSISTABLE_KEYS`. Export
`readFocusHoldSeconds(): number`, which returns
`normalizePersistedSettings(loadFromLocalStorage()).focusHoldSeconds` or the
default. In `AppearanceControls`, add an `AppearanceField` labelled
"Terminal focus hold" with the description "Seconds without input before a
focused terminal pane stops holding operator messages. 0 holds until focus
moves." and an `Input` of `type="number"`, `min={0}`, `step={1}`,
`inputMode="numeric"`, bound to `settings.focusHoldSeconds` and
`updateFocusHoldSeconds`. The field is UI work: the executor loads the
`impeccable` skill and follows `.impeccable.md`.

Docs: in `docs/guides/web-ui.md` under **Navigation**, a paragraph stating
that a focused web terminal pane holds operator messages addressed to its
seat until **Terminal focus hold** seconds pass without input or focus
leaves the pane, after which the daemon clears the composer and delivers;
switching to another application keeps the hold; `0` disables the timeout.

Test shape: `terminalFocusReport.test.ts` drives `createFocusReport` with a
fake socket (`readyState` `WebSocket.OPEN`, recorded `send`) and
`vi.useFakeTimers()`. `useTmuxSessions.test.ts` already records sent frames
(`sentMessages(ws, type)`) and takes and releases control (~1509-1551); its
new test grants the take, asserts one `terminal_focus` with
`focused: true`, then mocks `document.hasFocus` to return `false`, calls
`releaseControl`, and asserts no `terminal_release_control`. The existing
release tests (~1548, ~1648) stub `document.hasFocus` to return `true`,
because jsdom reports no focus for a hook rendered without a focused element.

Observed checks: none run for this leaf; the web files were read at
`ac3182b54f`. Planned, from `web/`:
`npx vitest run src/hooks/__tests__/terminalFocusReport.test.ts src/hooks/__tests__/useTmuxSessions.test.ts src/hooks/__tests__/useSettings.test.ts src/components/settings/sections/__tests__/AppearanceSection.test.tsx src/__tests__/settingsSliderFocus.test.ts src/components/activity/terminal/__tests__/TerminalTab.test.tsx`,
`npm run type-check`, and `npm run lint:js`.

**Granularity:** five hand-maintained production files
(`terminalFocusReport.ts`, `useTmuxSessions.ts`, `tmuxSessionMessages.ts`,
`useSettings.ts`, `AppearanceSection.tsx`) plus one guide; one lifecycle
owner (the focus report). Seven acceptance items cover one behavior, the web
report with its expiry and its preference, which cannot ship apart: a report
without expiry would hold wakes indefinitely, and an expiry without its
preference would leave the default unconfigurable. One commit, one test run.

Activation: web UI code; live after `uv run gobby ui build` rebuilds
`web/dist` and the browser tab reloads. No daemon restart and no binary
install, but ship only after the P1 restart (decision 27).

**Acceptance:**

- 3.1.1 - Once this attachment holds the lease, the report sends one `terminal_focus {focused: true}` for it, and a lease held by another attachment sends nothing. test: `web/src/hooks/__tests__/terminalFocusReport.test.ts::reportsFocusOnceWhenTheLeaseIsHeld`.
- 3.1.2 - After `holdSeconds` without activity the report sends `focused: false` and keeps the lease; the next `keydown`, `paste`, `pointerdown` or `wheel` sends `focused: true` again, and `pointermove` does not. test: `web/src/hooks/__tests__/terminalFocusReport.test.ts::idleExpiryReportsFocusLostAndActivityRestoresIt`.
- 3.1.3 - `holdSeconds` of `0` never expires, and losing the lease forgets the report without sending `focused: false`. test: `web/src/hooks/__tests__/terminalFocusReport.test.ts::zeroHoldNeverExpiresAndLosingTheLeaseForgetsTheReport`.
- 3.1.4 - `useTmuxSessions` reports focus after a granted take, and `releaseControl` sends no `terminal_release_control` while `document.hasFocus()` is false. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::reportsFocusAfterTheTakeAndKeepsTheLeaseOnWindowBlur`.
- 3.1.5 - `focusHoldSeconds` defaults to `300`, persists to localStorage without reaching the `ui_settings` payload, normalizes invalid values away, and `readFocusHoldSeconds` returns the stored value. test: `web/src/hooks/__tests__/useSettings.test.ts::focusHoldSecondsDefaultsPersistsLocallyAndReads`.
- 3.1.6 - Settings, Appearance, renders the "Terminal focus hold" seconds field and routes a change to `updateFocusHoldSeconds`. test: `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx::routesAFocusHoldChangeToUpdateFocusHoldSeconds`.
- 3.1.7 - The web UI guide documents the terminal focus hold and its setting. behavior: "Terminal focus hold" in `docs/guides/web-ui.md`.

## V1 Plan Changelog
`kind: framing`

- 2026-10-21: First narrative draft by plan-writer gobby#17295 from the
  #24769 research report and Josh's rulings. Design chosen: per-attachment
  boolean plus one reply-less `terminal_focus` verb; the lease-release
  alternative is recorded as rejected (decision 10); the Telegram feedback
  leaf is dropped (decision 18).
- 2026-10-21: Consensus between plan-writer gobby#17295 and plan-adversary
  gobby#17296 after two review rounds, reached on blob 8f22c01d64. Round 1
  moved the golden corpus entry and `GOLDEN_NAMES` into 2.1 (decision 20),
  made the focus-release path pass `require_unread=True` so an already-read
  DM is never replayed with a wipe (decision 12, acceptance 1.2.9), cleared
  the displaced holder's focus on takeover, fired the focus observer before
  `_notify_holder`'s early return, widened activity to any gclient input
  (decision 16) and fixed the landing order (decision 21). Round 2 found
  nothing further. No enhancer edits were received.
- 2026-10-21: Amendment under task #24784 by plan-writer gobby#17295 on
  Josh's ruling that web viewers count as focused (decision 22). Decision 9
  dropped the `gclient` viewer condition, 1.1.1 and 1.2.4 now cover a focused
  web holder, and P3 adds 3.1, the web terminal's `terminal_focus` report
  with the same idle clock, a `focusHoldSeconds` client setting defaulting to
  300, and a window switch that keeps the web lease (decisions 23-27). The
  adversary-derived M1 was withdrawn for re-derivation.
- 2026-10-21: Renewed consensus between plan-writer gobby#17295 and
  plan-adversary gobby#17296 on the #24784 amendment after one review round,
  reached on blob 73cde1e9fc. Round 1 added the `TerminalTabViewport.test.tsx`
  consumer entry, widened the Appearance target to the whole file, skipped
  the focus observer on the holder's repeated-take retry (`holder_changed`,
  acceptance 1.1.3), and named a deferred `hasFocus` check as the fallback if
  the real-browser window-switch check fails. No enhancer edits were
  received.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan
validation, none has run yet. Each runs after P1 lands with a daemon restart
and P2 lands with a gclient install, and each must hold before the epic
closes.

1. Plan validation, the only planning-time check:
   `uv run gobby plans validate .gobby/plans/pane-focus-wake-gating.md -p /Users/josh/Projects/gobby`
   must pass with no errors.
2. The tests named in the acceptance items of 1.1, 1.2, 2.1 and 3.1 must
   pass in each leaf's close validation, with `uv run ruff check src/`,
   `uv run mypy src/`, `cargo clippy -p gobby-client`, and `npm run
   type-check` and `npm run lint:js` in `web/` clean.
3. Focus a seat's pane in gclient and type, then send that seat a Telegram
   DM. The composer must not change, the daemon log must show the wake
   withheld as `pane_focused`, and the Telegram side must stay silent (no
   queued notice since `9f76e9f6e2`).
4. Click another pane. Within a second the first pane's composer must be
   cleared and `[Gobby] Check messages` submitted, and the agent must read
   the DM.
5. Repeat step 3, then leave the pane untouched for `focus_hold_seconds`
   (set `focus_hold_seconds = 10` in `~/.gobby/client/prefs.toml` and press
   `prefix+shift+r` to reload for the check). The same clear and submit must
   follow the timeout, and typing afterwards must land directly with no
   `○ observe` state and no second take.
6. Switch to another application while the pane is focused and active, then
   send a DM. The wake must stay held (window blur is ignored) until the idle
   timeout.
7. Send an ordinary `send_message` wake (not operator comms) to the focused
   seat with an empty composer. It must deliver as today: focus never holds a
   non-wiping wake.
8. Quit gclient while a wake is held. The finalized attachment must release
   the hold and the wake must deliver.
9. Repeat step 3, then submit your own prompt in the pane before the timeout
   so the seat's turn reads the DM through hook context. After the timeout
   nothing must be typed into the pane and the daemon log must show the
   release skipped as `mailbox_read`.
10. In the web UI, focus the same seat's terminal pane and type, then send
    the seat a Telegram DM. The composer must not change and the daemon log
    must show `pane_focused`. Click the chat input: the composer must be
    cleared and `[Gobby] Check messages` submitted.
11. Repeat step 10 with **Terminal focus hold** set to `10` in Settings,
    Appearance, and leave the pane untouched. The clear and submit must
    follow the timeout, and typing afterwards must land with no take-back
    prompt.
12. Repeat step 10, then switch to another application before the timeout.
    The wake must stay held until the timeout, and on return the pane must
    still hold control.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Lease registry focus state and the terminal_focus verb
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `_Attachment` carries `focused`, default `False`; `focus_held`
    is true only for a live holder with `focused` set, whether its viewer is `gclient`
    or `web`, and false for an unfocused holder and for a focused non-holder. test:
    `tests/terminals/test_lease_authority.py::test_focus_held_requires_a_focused_holder_of_either_viewer`.

    1.1.2: `release_control`, `finalize` and a `takeover` in `take_control` clear
    the released or displaced record''s `focused`; a stale attachment makes `set_focus`
    return `False`. test: `tests/terminals/test_lease_authority.py::test_release_finalize_and_takeover_clear_focus`.

    1.1.3: The focus observer fires synchronously with the terminal id on `set_focus(False)`
    of the holder and on every holder change from `_notify_holder`, with or without
    a holder observer installed, never on the holder''s repeated-take retry, and is
    never awaited. test: `tests/terminals/test_lease_authority.py::test_focus_observer_fires_on_focus_loss_and_holder_change`.

    1.1.4: `terminal_focus` reaches `set_focus` through the websocket type map, sends
    no reply, and is an off-loop message type. test: `tests/servers/test_terminal_ws_lease.py::test_terminal_focus_marks_the_holder_and_sends_nothing`.

    1.1.5: `message_classes.py` holds the message-class sets and predicates and `server.py`
    is under 850 lines. file: `src/gobby/servers/websocket/message_classes.py`.

    1.1.6: The protocol contract documents `terminal_focus`. behavior: "terminal_focus"
    row in `docs/contracts/gterm-protocols.md`.'
  labels:
  - covers:pane-focus-wake-gating:1.1:1.1.1
  - covers:pane-focus-wake-gating:1.1:1.1.2
  - covers:pane-focus-wake-gating:1.1:1.1.3
  - covers:pane-focus-wake-gating:1.1:1.1.4
  - covers:pane-focus-wake-gating:1.1:1.1.5
  - covers:pane-focus-wake-gating:1.1:1.1.6
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Operator comms wakes hold on a focused pane and dispatch on release
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: With a focused holder and a pending operator comms
    wake, `dispatch_live_wake` returns `skipped: "pane_focused"`, writes nothing to
    the terminal, records the hold, and the result is retryable. test: `tests/events/test_comms_wake.py::test_operator_comms_wake_holds_while_the_pane_is_focused`.

    1.2.2: `release_focus_hold` re-dispatches the held session once and the wake lands
    with `clear_before_submit=True`; a second release is a no-op. test: `tests/events/test_comms_wake.py::test_focus_release_dispatches_the_held_wake_with_a_wipe`.

    1.2.3: An open transcript turn withholds as `composer_unconfirmed` before the
    focus hold is consulted, and an `active` row withholds as `session_active`. test:
    `tests/events/test_comms_wake.py::test_active_turn_protection_outranks_the_focus_hold`.

    1.2.4: A focused `web` holder holds like a focused `gclient` holder, while an
    unfocused holder of either viewer, a registry-less dispatcher, and a pending wake
    that is not an operator comms wake all take today''s path. test: `tests/events/test_comms_wake.py::test_focus_hold_applies_only_to_comms_wakes_with_a_focused_holder`.

    1.2.5: The batch path returns `pane_focused` for a focused recipient and still
    sends to the others in one host exchange. test: `tests/events/test_wake_native_terminal.py::test_batch_wake_holds_a_focused_recipient_and_sends_the_rest`.

    1.2.6: `init_terminal_wiring` binds the lease registry to the dispatcher and the
    dispatcher''s `release_focus_hold` as the focus observer; the front-door branch
    binds neither. test: `tests/terminals/test_composition_roots.py::test_terminal_wiring_binds_focus_release_to_the_wake_dispatcher`.

    1.2.7: `wake_web_chat.py` carries the web-chat wake path and `wake.py` is under
    1,000 lines. file: `src/gobby/events/wake_web_chat.py`.

    1.2.8: The guides name `pane_focused` beside `composer_occupied`. behavior: "pane_focused"
    in `docs/guides/sessions.md` and `docs/guides/agents.md`.

    1.2.9: `release_focus_hold` for a held session whose mailbox has no undelivered
    message returns `skipped: "mailbox_read"`, writes nothing to the terminal and
    clears no composer. test: `tests/events/test_comms_wake.py::test_focus_release_after_the_mailbox_was_read_sends_nothing`.'
  labels:
  - covers:pane-focus-wake-gating:1.2:1.2.1
  - covers:pane-focus-wake-gating:1.2:1.2.2
  - covers:pane-focus-wake-gating:1.2:1.2.3
  - covers:pane-focus-wake-gating:1.2:1.2.4
  - covers:pane-focus-wake-gating:1.2:1.2.5
  - covers:pane-focus-wake-gating:1.2:1.2.6
  - covers:pane-focus-wake-gating:1.2:1.2.7
  - covers:pane-focus-wake-gating:1.2:1.2.8
  - covers:pane-focus-wake-gating:1.2:1.2.9
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: gclient reports pane focus and its idle expiry
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '2.1.1: After the focused pane''s take is granted, gclient
    sends one `terminal_focus {focused: true}` for that attachment and no second `terminal_take_control`.
    test: `crates/gclient/tests/lease_control.rs::idle_expiry_reports_focus_lost_and_activity_restores_it`.

    2.1.2: With `focus_hold_seconds = 1` and no activity, gclient sends `terminal_focus
    {focused: false}` for the held pane without releasing the lease; a keystroke then
    sends `focused: true` again and the key reaches `terminal_input`. test: `crates/gclient/tests/lease_control.rs::idle_expiry_reports_focus_lost_and_activity_restores_it`.

    2.1.3: `FocusHold::sync` never marks idle when `hold_seconds` is `0`, and a focused-pane
    change forgets the reported key without sending `focused: false`. test: `crates/gclient/src/app/live_loop/focus_hold.rs::zero_hold_never_expires_and_focus_moves_forget_the_key`.

    2.1.4: `focus_hold_seconds` round-trips through `prefs.toml` with default `300`,
    and an unknown key still fails to load. test: `crates/gclient/tests/startup.rs::prefs_round_trip_and_reject_unknown_keys`.

    2.1.5: The user guide documents `focus_hold_seconds` and the focus hold behavior.
    behavior: "focus_hold_seconds" in `docs/guides/gclient-user-guide.md`.

    2.1.6: gclient ships as `0.1.67` in `Cargo.toml`, `Cargo.lock` and the managed
    version pin. file: `src/gobby/install/version_pins.py`.

    2.1.7: `focus.json` is listed in the golden manifest after `release_control.json`,
    `GOLDEN_NAMES` lists it at the same position, and the Python corpus test round-trips
    it with 44 names and 45 files. test: `tests/servers/test_terminal_ws_golden.py::test_python_matches_terminal_ws_golden_corpus`.

    2.1.8: The gclient corpus test replays the 44-name manifest against `GOLDEN_NAMES`.
    test: `crates/gclient/tests/ws_golden.rs::corpus_replays_from_canonical_manifest`.'
  labels:
  - covers:pane-focus-wake-gating:2.1:2.1.1
  - covers:pane-focus-wake-gating:2.1:2.1.2
  - covers:pane-focus-wake-gating:2.1:2.1.3
  - covers:pane-focus-wake-gating:2.1:2.1.4
  - covers:pane-focus-wake-gating:2.1:2.1.5
  - covers:pane-focus-wake-gating:2.1:2.1.6
  - covers:pane-focus-wake-gating:2.1:2.1.7
  - covers:pane-focus-wake-gating:2.1:2.1.8
  tdd: true
  source_section: '2.1'
  implementation_domain: fullstack
- title: Web terminal reports pane focus and its idle expiry
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '3.1.1: Once this attachment holds the lease, the report sends
    one `terminal_focus {focused: true}` for it, and a lease held by another attachment
    sends nothing. test: `web/src/hooks/__tests__/terminalFocusReport.test.ts::reportsFocusOnceWhenTheLeaseIsHeld`.

    3.1.2: After `holdSeconds` without activity the report sends `focused: false`
    and keeps the lease; the next `keydown`, `paste`, `pointerdown` or `wheel` sends
    `focused: true` again, and `pointermove` does not. test: `web/src/hooks/__tests__/terminalFocusReport.test.ts::idleExpiryReportsFocusLostAndActivityRestoresIt`.

    3.1.3: `holdSeconds` of `0` never expires, and losing the lease forgets the report
    without sending `focused: false`. test: `web/src/hooks/__tests__/terminalFocusReport.test.ts::zeroHoldNeverExpiresAndLosingTheLeaseForgetsTheReport`.

    3.1.4: `useTmuxSessions` reports focus after a granted take, and `releaseControl`
    sends no `terminal_release_control` while `document.hasFocus()` is false. test:
    `web/src/hooks/__tests__/useTmuxSessions.test.ts::reportsFocusAfterTheTakeAndKeepsTheLeaseOnWindowBlur`.

    3.1.5: `focusHoldSeconds` defaults to `300`, persists to localStorage without
    reaching the `ui_settings` payload, normalizes invalid values away, and `readFocusHoldSeconds`
    returns the stored value. test: `web/src/hooks/__tests__/useSettings.test.ts::focusHoldSecondsDefaultsPersistsLocallyAndReads`.

    3.1.6: Settings, Appearance, renders the "Terminal focus hold" seconds field and
    routes a change to `updateFocusHoldSeconds`. test: `web/src/components/settings/sections/__tests__/AppearanceSection.test.tsx::routesAFocusHoldChangeToUpdateFocusHoldSeconds`.

    3.1.7: The web UI guide documents the terminal focus hold and its setting. behavior:
    "Terminal focus hold" in `docs/guides/web-ui.md`.'
  labels:
  - covers:pane-focus-wake-gating:3.1:3.1.1
  - covers:pane-focus-wake-gating:3.1:3.1.2
  - covers:pane-focus-wake-gating:3.1:3.1.3
  - covers:pane-focus-wake-gating:3.1:3.1.4
  - covers:pane-focus-wake-gating:3.1:3.1.5
  - covers:pane-focus-wake-gating:3.1:3.1.6
  - covers:pane-focus-wake-gating:3.1:3.1.7
  tdd: true
  source_section: '3.1'
  implementation_domain: frontend
```
