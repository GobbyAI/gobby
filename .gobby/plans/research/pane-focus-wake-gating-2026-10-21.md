# Pane-focus-gated comms wakes with daemon composer wipe on focus loss

Task: #24769. Researcher session gobby#17246. Checkout `/Users/josh/Projects/gobby`, branch
`0.5.0`, HEAD `a0f509833d`. Read-only research; no production, test or crate files changed.

Josh's idea (Telegram, 2026-10-20 23:19 CDT): while a pane has focus, hold wake messages in
the queue. When the pane loses focus, the daemon wipes the pane's composer and sends the wake.
Focus state clears x minutes after the last activity. Addendum 23:21: "So that would be a
change to gclient." Addendum ~23:30: "If we don't [track focus] we'd need to add it to the
payload we send when the pane states are polled."

## Summary

- **The daemon already knows in-app pane focus.** gclient's input lease follows pane focus.
  Focusing a pane sends `terminal_take_control`, and moving focus away sends
  `terminal_release_control`. The daemon keeps the holder in memory in
  `TerminalLeaseRegistry`. No new gclient message is needed for focus moves inside a window.
- **OS window focus is tracked nowhere.** gclient never enables terminal focus reporting
  (DECSET mode 1004). It also drops the focus-in and focus-out events that its input parser
  already decodes. Alt-tabbing to a browser leaves the lease held. This is the real gclient
  change.
- **gclient does not poll pane state.** Its only daemon pulls are on-demand inventory pages,
  and its only timer is a local render tick. Focus should travel as an event on the existing
  websocket, not as a field in a poll payload.
- **No wall-clock last-activity time exists.** The composer ledger and the lease attachment
  are both sequence-only. The x-minute expiry needs a new daemon-side timestamp.
- **Recommendation:** gate operator comms wakes on a "fresh focus holder" read in the wake
  dispatcher. Release the hold from the lease-holder observer (focus loss), from a new
  gclient window-blur message, and from an expiry timer. Keep active-turn protection first
  and let the #24768 wipe run unchanged at dispatch time.

## Current behavior (with file and line evidence)

Line numbers are at HEAD `a0f509833d` unless marked staged. The #24768 change is staged and
uncommitted in the main checkout, so its lines will drift when it lands.

### What gclient reports about focus

1. **Focus hints (persisted, not live focus).** gclient offers the window's shown
   `(project, tab, pane)` when it changes (`crates/gclient/src/app/live_loop/focus_hints.rs:64-88`)
   as `WorkspaceOp::WorkspaceSetFocusHints` (`focus_hints.rs:91-103`). The module doc says it
   exists "so the next window opens where this one left off" (`focus_hints.rs:1-3`). The
   daemon handles it in `workspace_set_focus_hints` (`src/gobby/terminals/workspace_ops.py:172-189`)
   and writes DB columns `workspaces.focused_project_id`, `workspaces.focused_tab_id` and
   `workspace_tabs.focused_pane_id` (`src/gobby/storage/workspaces.py`, `set_focus_hints`,
   UPDATEs at about lines 818-842). The row has no focus timestamp and no per-window
   identity; the last writer wins. It is unusable as a focus gate.
2. **Input lease follows pane focus (live, in memory).** `move_live_focus` releases the
   previously focused pane (`crates/gclient/src/app/live_loop/control.rs:56-65`).
   `focus_live_pane` then requests control of the new pane unless a peer holds it
   (`control.rs:37-45`). The request goes out as `terminal_take_control`
   (`crates/gclient/src/app/live_control.rs:63-135`). The release is
   `terminal_release_control` (`control.rs:241-260`), queued behind the pane's accepted
   writes. Release on focus loss fires only when the pane held the lease or had a grant in
   flight (`release_live_control`, `control.rs:191-214`).
3. **Daemon lease registry.** `TerminalLeaseRegistry.take_control` sets `_Lease.holder` to
   the attachment (`src/gobby/terminals/leases.py:449-485`). `release_control` clears it
   (`leases.py:487-508`). Socket loss finalizes attachments (`leases.py:510-583`). Take and
   release call the holder observer inside the terminal's lease lock (`leases.py:438-447`
   called under `async with self.lock(...)`). The single observer is wired in
   `src/gobby/runner_init/terminal_wiring.py:121-132`, where it syncs the gterm input grant.
   `_Attachment` records viewer (`"web"` or `"gclient"`), backend and geometry but no
   timestamp (`leases.py:125-143`). `_Lease` holds only holder, generation and sizing owner
   (`leases.py:146-150`).
4. **OS window focus is dropped.** gterminal's input parser decodes `ESC [ I` and `ESC [ O`
   as `RawInputEvent::OuterFocusGained` / `OuterFocusLost`
   (`crates/gterminal/src/raw_input.rs:136-137`, `:439-440`). gclient's terminal guard has no
   focus-change mode: the `ModeBackend` trait covers raw mode, keyboard enhancement,
   alternate screen, bracketed paste, cursor and mouse capture only
   (`crates/gclient/src/teardown.rs:14-74`). `route_live_input` handles host colour, paste,
   mouse, keys and text, and has no focus arm (`crates/gclient/src/app/live_loop.rs:677-760`).
   A test asserts focus-lost yields neither key nor text bytes
   (`crates/gclient/tests/terminal_links.rs:76-77`).
5. **No polling.** `terminal_list` is an on-demand inventory page request
   (`crates/gclient/src/daemon/live_connect.rs:169-180`) used by relist and orphan discovery
   (`crates/gclient/src/app/live/relist.rs:147`). The live loop's only interval is the render
   tick (`crates/gclient/src/app/live_loop.rs:155`).

### What the daemon knows about typing

- gclient keystrokes go to the gterm host directly, not through the daemon
  (`src/gobby/terminals/write_coordinator.py:685-699`, docstring of `observe_operator_input`).
- The host emits `input_activity` with kind, submit, interrupt and effect
  (`src/gobby/terminals/host_events.py`, decoder around line 93;
  `docs/contracts/gterm-protocols.md:109-153`). The daemon sink is `_observe_input_activity`
  (`src/gobby/servers/websocket/server.py:367-382`). It records mediated input for turn
  observation and lifts the automatic-write quarantine.
- `ComposerLedger` derives composer state from those events
  (`src/gobby/terminals/composer_ledger.py:1-14`, `observe_host_event` at `:162`). It lives in
  daemon process memory (`:119-128`), snapshotted to `~/.gobby/runtime/composer_ledger.json`
  (`:544-588`). Entries are ordered by ledger sequence; `_Entry` has no wall-clock activity
  time (`:102-116`).

### Wake dispatch path

- `dispatch_mailbox_wakes` (`src/gobby/sessions/mailbox_delivery.py`) calls
  `WakeDispatcher.dispatch_live_wake` (`src/gobby/events/wake.py:273-292`), which runs
  `_dispatch_live_wake_unlocked` (`wake.py:499-650`). Multi-recipient native wakes go through
  `src/gobby/events/wake_batch.py`, which calls the same `_composer_blocks_wake`
  (`wake_batch.py:108`). One gate therefore covers both paths.
- **Active-turn protection, first layer:** an `active` session row returns
  `skipped: "session_active"` unless priority is urgent (`wake.py:542-561`;
  `wake_batch.py:77` for batches).
- Managed terminal rows go to `_send_managed_terminal_wake` (`wake.py:769-863`). It holds
  `composer_action_lock(terminal_id)` across preflight, probe and write, then sends with
  `clear_before_submit=not confirmed_empty` (`wake.py:808`).
- `_composer_blocks_wake` (`wake.py:711-767`) is the gate:
  - **Active-turn protection, second layer:** an open transcript turn withholds as
    `composer_unconfirmed` (`wake.py:752-753`).
  - **Staged #24768:** if any undelivered wake message is an operator comms wake, return
    "deliver, not confirmed empty". That forces the clear-before-submit wipe even over a
    human draft (`wake.py:754-758` staged; predicate in staged
    `src/gobby/events/comms_wake.py:11-25`).
  - `empty`/`held` deliver, `draft` withholds as `composer_occupied`, anything else withholds
    as `composer_unconfirmed` (`wake.py:759-767`).
- Withheld wakes are retried, never dropped. `RETRYABLE_WAKE_SKIPS` is `session_active`,
  `composer_occupied`, `composer_unconfirmed` (`src/gobby/events/live_wake.py:27-32`).
  `_schedule_wake_followup` routes them (`wake.py:309-328`). Composer retries back off from
  15 s to 240 s (`wake.py:80-81`, `:343-403`).
- Automatic daemon writes ignore the lease holder. `_blocked_automatic` checks only the
  quarantine and unresolved keys (`src/gobby/terminals/write_coordinator.py:624-640`); the
  lease is revalidated only for `origin == "operator"` (`write_coordinator.py:642-660`). This
  is why a wake can type into a pane Josh is typing in.

### Staged #24768 (P0, claimed by session 4c302de0, uncommitted)

- `MailboxService.send(..., replace_composer=True)` is allowed only for system-originated,
  direct-session, wake-requested sends. It stamps `metadata.operator_comms = True` and strips
  any caller-supplied copy (staged `src/gobby/sessions/mailbox.py`, around lines 135-200).
- Telegram ingress sets `replace_composer` only for the enrolled operator's private
  conversation attached to that session (staged `src/gobby/communications/session_delivery.py:13-33`;
  staged `src/gobby/communications/telegram_actions.py`, around lines 175-205). It also tells
  Josh when the message was queued instead of delivered.

## Focus signal options

| Option | Source | Exists today | Gap |
| --- | --- | --- | --- |
| A. Lease holder as focus | gclient take/release on pane focus, `leases.py:449-508` | Yes, in daemon memory | Blind to OS window blur; no timestamp |
| B. OS window focus (DECSET 1004) | Outer terminal focus-in/out, parsed in `raw_input.rs:439-440` | Parsed, never enabled or handled | gclient must enable mode 1004 and send a new message |
| C. `input_activity` recency | gterm host events, `server.py:369` | Events yes, time no | Daemon must stamp a wall-clock time per terminal |
| D. Persisted focus hints | `workspace.set_focus_hints`, DB rows | Yes | Seed for the next window, last writer wins; wrong semantics |
| E. tmux `focus-events` and `pane-focus-in`/`pane-focus-out` hooks | tmux | No use in repo (no matches for these names) | Covers only raw tmux clients, not gclient |
| F. A field in a polled pane-state payload | Josh's addendum | No such poll exists | Would add a poll loop the event channel makes unnecessary |

Option A gives exact in-app focus with no gclient change. Option B closes the window-blur hole
and is the gclient change Josh anticipated. Option C supplies the clock for "x minutes after
the last activity". D, E and F are rejected: D has the wrong semantics, E misses gclient and
web viewers, and F adds polling the websocket already makes unnecessary.

## Proposed design

**Definition.** A terminal is *focus-held* when its lease holder is a live attachment, that
attachment's window has OS focus, and the last focus or input activity on it is within
`focus_hold_seconds` (Josh's "x minutes"; proposed default 300 s, in daemon config).

1. **gclient: report window focus (the gclient change).**
   - Add `enable_focus_change` / `disable_focus_change` to `ModeBackend`
     (`crates/gclient/src/teardown.rs`). The crossterm backend writes `ESC [ ? 1004 h` / `l`.
     Add a teardown obligation so the mode always resets on exit or suspend.
   - In `route_live_input`, handle `OuterFocusGained` / `OuterFocusLost`. Send one
     `terminal_window_focus {attachment_id, focused: bool}` for the focused live pane only,
     through the same per-pane writer barrier as release, so ordering against typed bytes
     holds.
   - Leave lease take/release unchanged. A window blur is not a lease release, because the
     operator expects to type immediately on return.
2. **Clock owner: gclient (revised after Josh's 23:38 question).** gclient sees every
   keystroke for its focused pane before gterm does and is the only process that sees OS
   window focus. It keeps a per-window idle timer in memory (`focus_hold_seconds`, default
   300 s, configurable; Josh approved 23:40 CDT). It sends `terminal_window_focus
   {focused: false}` on blur, on idle expiry and before suspending (the suspend branch at
   `crates/gclient/src/app/live_loop.rs:215-225` already exists). It sends
   `{focused: true}` on refocus or the next keystroke. The daemon stores only
   `window_focused: bool` on `_Attachment` (`leases.py:125-143`), so it needs no clock and
   no timer. A gclient crash or socket loss already finalizes the attachment and clears the
   holder (`leases.py:510-583`), which releases the hold. gterm is rejected as the owner: it
   sees the same keystrokes but not window focus or wakes, and owning the clock there needs
   a new host-protocol event for no gain. A daemon clock was the original proposal; it
   works, but it duplicates state gclient already has. Residual gap: a hung gclient with a
   live socket would hold wakes until the socket drops.
3. **Daemon: hold gate.** In `_composer_blocks_wake`, after the turn-in-flight check
   (`wake.py:752`) and before the #24768 comms check (`wake.py:755` staged): if an operator
   comms wake is pending and the terminal is focus-held, return a new withheld result
   `skipped: "pane_focused"`. Add it to `RETRYABLE_WAKE_SKIPS` (`live_wake.py:32`). Scope the
   hold to operator comms wakes, because they are the only wakes that wipe. Ordinary agent
   wakes keep the existing draft-protection path.
4. **Daemon: dispatch on focus loss.** Three triggers schedule a retry for each session whose
   terminal has a held wake:
   - the holder becomes `None` or another attachment, via the holder observer
     (`terminal_wiring.py:121-132`);
   - `terminal_window_focus {focused: false}`;
   - idle expiry, which arrives from gclient as `{focused: false}`; the daemon runs no timer.

   The observer runs under the lease lock (`leases.py:449-508`), and dispatch takes
   `composer_action_lock` and the wake lock. The observer must therefore only schedule a
   retry, never dispatch inline. The 15 s to 240 s backoff stays as the fallback if an event
   is lost.
5. **Wipe and send.** On retry, the gate sees no focus hold, reaches the #24768 comms branch,
   and `_send_managed_terminal_wake` sends with `clear_before_submit=True` (`wake.py:803-812`).
   No new wipe mechanism is needed.
6. **Visibility.** The staged Telegram feedback already reports withheld reasons. Add
   `pane_focused` with advice such as "Delivered when you leave the pane or after N minutes
   idle."

## Interaction with #24768 and active-turn protection

- **Order of checks:** session `active` row (`wake.py:542-561`), then open transcript turn
  (`wake.py:752`), then the new `pane_focused` hold, then the #24768 comms wipe (`wake.py:755`
  staged), then the ledger state. Both active-turn layers stay ahead of the hold, so a wake
  never steers a running turn.
- **#24768 is unchanged in mechanism.** It decides *whether* a comms wake may wipe. Focus
  gating decides *when*. Without the hold, #24768 wipes Josh's in-progress draft the moment a
  Telegram DM arrives. With the hold, the wipe moves to focus loss or idle expiry.
- **Land after #24768.** The hold sits inside the function #24768 edits and reuses
  `is_operator_comms_wake`. Build on the landed tip rather than editing in parallel.
- **Draft semantics.** An ordinary `draft` still withholds as `composer_occupied`. For
  operator comms wakes, #24768 already overrides that; this design keeps the override and
  only adds the focus delay.

## Risks

- **Short blur wipes a draft.** Tabbing to another pane for two seconds is focus loss, so the
  daemon wipes and sends. Josh accepted wipe-on-wake; this timing is new. A short grace delay
  after focus loss (for example 5 s, cancelled by re-focus) reduces surprise. Josh decides.
- **Terminals without mode 1004.** They never send focus-out. The expiry timer covers this
  case, at x minutes' latency.
- **Unattended focused window.** A gclient left focused on the Assistant pane holds the lease
  indefinitely. Expiry on last activity handles it, so `focus_hold_seconds` must be bounded.
- **Ephemeral state.** A daemon restart drops focus records. Pending wakes then dispatch on
  the first post-restart attempt unless gclient re-takes control first. That is acceptable
  but will look like an immediate wipe after a restart.
- **Web viewers** can hold the lease (`Viewer = Literal["web", "gclient"]`, `leases.py:38-39`)
  but send no window-focus message. Treat a web holder as focused, subject to expiry, until
  the web UI reports page visibility.
- **Raw tmux clients** have no lease and no hold. Their wakes behave as today.
- **Lock ordering.** Dispatching from the holder observer under the lease lock would stall
  `take_control`. The design only schedules from there.
- **Lost focus-out event.** The existing backoff retry bounds the wait at 240 s per attempt.

## Recommendation

Build Options A, B and C together. Use the existing lease as the in-app focus signal. Add OS
window focus reporting in gclient. Add a daemon wall-clock stamp for expiry. Gate only
operator comms wakes, after both active-turn checks and before the #24768 wipe. Do not add a
polling payload; the websocket already carries focus as events. Land after #24768. Josh
decided the expiry: 300 s, configurable (23:40 CDT). gclient owns the idle clock (see
Proposed design item 2). Open decision: whether a focus-loss grace delay applies
(proposed 5 s).

## Proposed tasks

1. **gclient: report outer window focus.** Enable and reset DECSET 1004 through `ModeBackend`
   and its teardown obligations. Route `OuterFocusGained`/`OuterFocusLost` in
   `route_live_input` to a `terminal_window_focus` message for the focused live pane, ordered
   through the pane writer. Tests: `RecordingBackend` records enable and reset; focus events
   produce exactly one message and no pane bytes. gclient patch version bump.
2. **Daemon: attachment focus record and websocket verb.** Add `window_focused` and
   `last_focus_activity_at` to `_Attachment`; stamp on take, window focus and holder
   `input_activity`; add `focus_held()`; register `terminal_window_focus` in the websocket
   server and document it in the terminal websocket contract. Tests in `tests/terminals/` for
   stamping, expiry and stale attachments.
3. **Daemon: `pane_focused` wake hold.** Add the gate in `_composer_blocks_wake` after
   `turn_in_flight_fingerprint`, scoped to pending operator comms wakes. Add the skip to
   `RETRYABLE_WAKE_SKIPS`. Tests in `tests/events/`: held while focused, delivered with a
   wipe after release, an active turn still wins, batch path also held.
4. **Daemon: focus-loss and expiry release.** Extend the holder observer and the window-focus
   handler to schedule retries for held sessions, plus a per-terminal expiry timer. Never
   dispatch under the lease lock. Tests: release, takeover by another attachment, blur and
   expiry each deliver once; a lock-order test.
5. **Telegram feedback for `pane_focused`.** Extend the staged #24768 feedback text. Test in
   `tests/communications/test_telegram_actions.py`.

Tasks 2 to 5 depend on #24768 landing. Task 1 is independent and fits Lane 8 (client chrome)
in parallel.
