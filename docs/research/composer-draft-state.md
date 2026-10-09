# Composer And Draft State Without Screen Reading (#23712)

Research evidence for #23712, requested by Josh on 2026-10-08 ("Find a better
solution on 23712. Research it."). Goal: know whether a CLI composer holds a human
draft, or deliver without typing, with no screen regex and no risk of typing into a
human draft. Transcript turn state stays the idle authority. Ruling 1b (one Codex
`/compact` resubmit) stands unless the design below supersedes it.

Sources fetched by the author are cited by URL or `file:line`. Claims reported by a
research subagent and not re-read are marked *(subagent-reported)*.

## Why The Pane Probe Fails

- 15471 (Codex), 2026-10-08 after #23791 went live: the 10:39:34 CT wake was declined
  `composer_unconfirmed`. Its pane snapshot at 10:40 held only a blank line and
  `› Ask Codex to do anything`, with no footer rows (Orchestrator capture).
- 15471 compact attempt `fc387d0a` refused at 10:42:41 CT with "composer could not be
  confirmed empty". The rollout shows that turn still running: `task_complete` at
  `2026-10-08T15:42:46.714Z`. Codex is not in `_SETTLE_BEFORE_REFUSAL_SOURCES`
  (`src/gobby/mcp_proxy/tools/sessions/_terminal_compaction.py:79`), so the first
  unknown read was final.
- 15677 (Claude) refused `/compact` as composer unknown at 03:34, 04:40 and 04:54 CT
  while its transcript showed the turn settled; a human typed `/compact` each time.

## What Each Surface Exposes

### Claude Code

- No hook event or field carries composer contents
  (<https://code.claude.com/docs/en/hooks.md>, event list checked). The statusLine
  input has no composer field *(subagent-reported,
  <https://code.claude.com/docs/en/statusline.md>)*.
- A typed slash command is written to the transcript as a user row at submit:
  15677 wrote `'/compact'` at `2026-10-08T08:39:00.828Z`, then its `compact_boundary`.
  Whether `UserPromptSubmit` fires for built-in slash commands is not documented.
- Channels push messages into a running session without the composer and wake an
  idle session. They are a research preview: a custom server needs
  `--dangerously-load-development-channels` and a startup consent dialog, the
  `channelsEnabled` org policy applies, and they cannot run `/compact`
  (<https://code.claude.com/docs/en/channels-reference.md>). Not recommended now.
- Auto-compact window: `--autocompact <window>`, `autoCompactWindow`, or
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100K to 1M); 1M-window models otherwise compact
  at about 967K (<https://code.claude.com/docs/en/model-config.md>,
  <https://code.claude.com/docs/en/env-vars.md>). This is a composer-free lead for
  mode 4 (15677 sat at 292K). Open question: Gobby stages the handoff before its own
  `/compact`; a native auto-compact fires `PreCompact` with trigger `auto` instead.

### Codex (installed `codex-cli 0.160.0`; shared daemon 0.161.0)

- App-server JSON-RPC offers `turn/start`, `turn/steer`, `turn/interrupt`,
  `thread/compact/start` and `thread/status/changed` (idle/active)
  (<https://learn.chatgpt.com/docs/app-server.md>). None of them touch a composer.
- `codex queue --session <uuid> "message"` (v0.149.0, PR #39092) sends to an existing
  session on the shared local app-server: an idle session starts a new turn, and a
  running one gets it as the next user turn
  (<https://codex.danielvaughan.com/2026/08/29/codex-cli-v0149-multi-session-agents-dashboard-codex-queue-working-directory/>).
- Seat threads appear to run on the shared daemon (inferential):
  - `codex app-server --listen unix://` (0.161.0) is running.
  - `~/.codex/app-server-daemon/daemon.stderr.log` holds `codex_core::tools::router`
    hook-block errors from seat tool calls (for example at `2026-10-08T15:53:03Z`).
  - Seat TUIs run as `codex resume <thread-id> --yolo` (15471 is `01a10cd2-5535…`).
  - Rollout `UserMessage` items carry a `client_id`.
- No surface reports TUI composer or draft state (app-server.md, not covered).

### Gobby's terminal layer

- All spawned seats are native (`NATIVE_FIRST_BACKEND`, `src/gobby/terminals/lifetime.py:14`);
  tmux rows come only from discovery *(subagent-reported)*.
- The gterm host owns the PTY. Direct gclient input reaches it on the frames socket,
  is checked against the input grant, and emits `input_activity` with `kind`,
  `bytes` and an `interrupt` flag (`crates/gterminal/src/host/events.rs:33-43`,
  `crates/gterminal/src/host/write.rs:165-180`). The daemon consumes it in
  `_observe_input_activity` (`src/gobby/servers/websocket/server.py:345`).
- Web-UI and proxied human input crosses the daemon as
  `WriteRequest(origin="operator")`; daemon writes are `daemon`/`automatic`
  (`src/gobby/terminals/write_coordinator.py`, `_blocked_automatic`,
  `observe_operator_input`). The daemon therefore sees every human input origin.
- Host event-ring gaps are detected but not replayed *(subagent-reported)*.
- tmux exposes no keystroke observation; nothing in `src/gobby` uses `pipe-pane` or
  client hooks *(subagent-reported)*.
- Every provider fires a submit hook into Gobby (`BEFORE_AGENT`:
  `src/gobby/install/*/hooks-template.json`, `src/gobby/hooks/events.py:469-485`).

### Grok

Not surveyed (the research agent stopped on an account spend limit). Today Grok has
no composer rules, so it gets no live typing; messages arrive through hook context.

## Recommended Design

Never read the screen. Know the composer from input provenance, and deliver without
typing where the provider supports it.

1. **Composer ledger in the daemon (all providers, native panes).**
   - Per terminal, the daemon records human input from `input_activity` (direct
     gclient) and operator-origin writes (web/proxy). Daemon writes are not human.
   - The composer is clean when no human input arrived after the provider last
     recorded a submit (the `BEFORE_AGENT` hook, or a transcript user row for slash
     commands). A submit empties the composer, so earlier bytes cannot remain.
   - Hook-latency race: a keystroke between Enter and the hook would read as
     consumed. Close it with one small host change: an `input_activity.submit` flag
     (chunk contains CR outside bracketed paste). The clean point is the last flagged
     chunk before the submit record; later chunks stay dirty.
   - Dialog input: human input while a displayed structured wait is open
     (`awaiting_input`/`awaiting_approval`, `docs/research/session-turn-lifecycle-matrix.md`)
     and resolved by its matching answer belongs to the dialog, provided the ledger
     was clean when the wait opened. Residual risk: keystrokes that straddle the
     wait's close.
   - An event gap, host epoch change, or interrupt key (Esc or Ctrl+C; a provider
     may restore an interrupted prompt) reads as dirty until the next submit.
   - Liveness valve: a dirty ledger with no submit raises an attention item naming
     the seat, and an explicit operator "release to automation" action (gclient key
     or web button) resets it. The human vouches; nothing is guessed.
   - Check-then-write race: optional host hardening lets `write_batch` carry the
     expected human-input sequence and refuse if direct input arrived since.
   - tmux panes get no live typing (durable messages via hook context), as Grok
     does today.
   - Draft present means refuse without typing; this keeps Josh's 09-29 policy,
     now exact instead of guessed.
2. **Codex delivery through the app-server (stage 2, behind gates).** Wakes become
   `turn/start` or `codex queue`; compaction becomes `thread/compact/start`. Nothing
   is typed, so modes 1 and 7 cannot occur for Codex and ruling 1b is superseded for
   Codex. Gates before adoption:
   1. A read-only `thread/list` on the app-server returns the seat thread ids
      (needs the Orchestrator's authorization; Josh's live daemon was not probed).
   2. Identify which socket speaks JSON-RPC
      (`~/.codex/app-server-daemon/app-server-control.sock` may be lifecycle-only;
      `remoteControlEnabled` is `false`).
   3. Confirm both launch modes attach: `codex resume` seats and srt-sandboxed
      spawned seats.
   4. On a scratch seat, an external `turn/start` renders in the TUI and leaves a
      composer draft untouched.
   5. On the same TUI-attached scratch seat, `thread/compact/start` writes the rollout
      `compacted` record that the existing boundary waiter reads. It never targets a
      live seat.

Transcript turn state (`transcript_cursor.turn_settled`) remains the authority for
idle and turn completion, and the pane probe is removed from the wake gate and the
compaction sender.

## Provider Limit Menu

- Evidence: at 10:56 CT on 2026-10-08, 17 Claude panes sat on the
  `/rate-limit-options` selector after Josh's usage limit, and only Esc plus a
  continue line cleared them. Session 6c556ffb wrote a synthetic assistant row at
  `2026-10-08T15:56:15.337Z` (`isApiErrorMessage: true`, `error: "rate_limit"`,
  `apiError: "usage_limit_reached"`, text "You've hit your monthly spend limit …
  resets Oct 11 at 11pm"), followed 10 ms later by a `turn_duration` row. The
  transcript therefore reads the turn as settled while the pane shows a menu.
- Risk: `_stop_failure_error` (`src/gobby/hooks/event_handlers/_misc.py:49`)
  classes every `rate_limit` as retryable, and `_resume_provider_failure` sends
  `PROVIDER_ERROR_RESUME_PROMPT` 1, 2 and 4 s later. Today only the pane probe
  withholds that wake. Option 3 in the menu is "Add funds".
- Design (Orchestrator ruling, 2026-10-08): a Claude StopFailure `rate_limit`, or a
  transcript `apiError: "usage_limit_reached"`, becomes `error_type="usage_limit"`
  with `retryable=False`. This records the provider error and raises an attention
  item. The ledger blocks every automatic write to that terminal until the next
  provider submit record, which is the operator's continue line, or a release. No
  key is ever automated into a limit menu.

## Stage 1 Implementation Plan (approved 2026-10-08)

- `src/gobby/terminals/composer_ledger.py` (new, about 300 lines). An in-memory
  ledger per terminal, owned by `WriteCoordinator` as `coordinator.composer_ledger`
  and reached through `get_app_context().write_coordinator`. Each entry keeps
  `session_id`, `clean_seq`, `human_seq`, an unsafe marker (`interrupt`, `gap`,
  `unverified`, `provider_limit`) with its sequence number, the submit-flagged
  write sequence numbers, the pending daemon text `(seq, text)`, and the dialog-input
  sequence number. One monotonic ledger sequence orders all events.
- Read result `LedgerRead(state, reason, pending)`:
  - `blocked`: an unsafe marker newer than `clean_seq`, or an untracked terminal.
    Untracked terminals include tmux rows and seats spawned before LAND.
  - `draft`: human input newer than `clean_seq`. Refuse without typing.
  - `held`: daemon text newer than `clean_seq`. The same payload is resubmitted with
    bare Enter (ruling 1b); a wake with a different payload clears first.
  - `empty`: none of the above.
- Observations:
  - Host `input_activity` events, through `_observe_input_activity`, are human input.
    An interrupt key sets the unsafe marker. Input while a structured wait is open is
    attributed to the dialog only if the entry was clean when the wait opened. On
    `resolve_wait`, a resumed wait consumes it and any other outcome turns it into
    human input.
  - Coordinator writes:
    - `operator` and `attention` origins are human input. Their submit is flagged:
      `submit=True`, an `enter` key, or CR in an input payload.
    - `automatic` and `daemon` text sets the pending text.
    - A submit or `enter` from those origins is flagged.
    - A clear sequence drops the pending text.
- Submit records: `BEFORE_AGENT`, `PRE_COMPACT` (manual) and `SESSION_START`
  (clear/compact). The clean point becomes the last flagged write at or before the
  record. If any human input since the clean point lacked a host `submit` field (an
  old host), the record itself becomes the clean point. A record with no new flagged
  write proves nothing and does not move the clean point.
- Spawned terminals are tracked clean at spawn. The operator valve
  `release_composer`, available as an HTTP route and a gobby-sessions tool, sets the
  clean point to now. It refuses a self-call from the target session, and the
  attention item names it. There is no web button (Orchestrator ruling).
- Persistence: a machine-local JSON state file holds the per-terminal states and the
  host event cursor `(epoch, seq)`. The file is rewritten atomically at most once
  per second when it has changed. At startup the ledger restores it. A one-shot
  stream `since=cursor` then replays input events up to the inventory snapshot seq
  in `recover_event_gap` (`src/gobby/terminals/host_event_reader.py`). A replay gap
  marks the entries `gap`. A new host epoch drops the old host's entries, whose
  terminals died with it, so they read untracked. A spawn committed on the new host
  before the reader resubscribes moves the ledger to that host at seq 0, and the
  reader replays the host from its start. Events from another host are ignored.
  gterm upgrades carry the epoch and ring (`HostEvents::restore`), so they do not
  dirty seats.
- Gates (net-neutral edits; wake.py, compact_continuation.py and the other host
  files are near the 1,000-line ceiling):
  - `probe_terminal_activity` (`src/gobby/runner_init/wake_activity.py`): the composer
    state comes from the ledger, and in-flight turns from transcript `turn_settled`.
  - `composer_gate_for_write` (`src/gobby/terminals/pane_io.py:404`) reads the ledger
    instead of a snapshot.
  - `interactive_capacity.py` replaces its composer read.
  - `codex` joins `_SETTLE_BEFORE_REFUSAL_SOURCES`.
- Host hardening, a separate Rust commit whose promotion Josh decides:
  - `NativeInput::submit()` reports CR outside bracketed paste, tracking paste state
    per slot.
  - `InputActivity.submit` is added.
  - Update the wire golden `control_input_activity.json`,
    `crates/gterminals/src/control.rs` and `protocol_contract.rs`.
  - The Python decoder accepts a missing field.
- First deploy: seats spawned before LAND read `unverified`. At LAND, Josh gives one
  go and the Assistant releases each seat (Orchestrator ruling). The optional
  `write_batch` expected-sequence hardening is excluded.

### Deviations in the implementation

- Ownership: terminal wiring binds the daemon's ledger with `bind_composer_ledger`,
  and gates read it through `read_composer(terminal_id)`. The coordinator records
  its own writes, and `NativeTerminalRuntime.write_batch` records native pane
  writes, so each write is counted once.
- Seats spawned before LAND, tmux rows and an unbound ledger read `blocked`
  (`untracked`), and gates treat that as `unknown`. There is no `unverified`
  marker.
- `_SETTLE_BEFORE_REFUSAL_SOURCES` is gone. The gates read no frame, so a
  frame painted mid-turn can no longer refuse a write, and no settle wait is
  needed before a refusal.
- Every daemon key outside the clear sequence, including an interrupt, leaves
  `held` with unknown text, because an interrupt can restore the prompt. The next
  gate reads it as stale daemon text and drains it.
- Clear keys are inert in the ledger. A clear key removes an unknown amount of text
  (Codex binds no line kill, and one backspace removes one character), so it only
  makes held text unknown. A completed drain empties the entry through
  `record_composer_drain`, which keeps a human draft and a block.
- `composer_drain_keys` sizes every drain: one backspace per character of held
  daemon text, then the standard pass. `clear_composer` and the four coordinator
  sites (the wake batch, the wake clear, the capacity reprompt and the idle
  reprompt) use it. The standard pass alone holds 8 backspaces, which left longer
  Codex text in front of the next write.
- A drain of unknown size refuses only on a positive draft frame (Orchestrator,
  16:24). Examples are the drain after an interrupt and the stale continuation drain.
  `clear_composer` polls the frame when the CLI has a reader, and a `held` or
  `changed` verdict refuses. An empty or unreadable frame records the drain and
  proceeds. Grok has no reader, so it drains blind and records the drain.
- The continuation re-pastes a dropped prompt (Orchestrator, 16:16). It sends
  Enter, and when BEFORE_AGENT does not arrive, it drains the held copy with a sized
  drain. It re-pastes only on a `left` verdict, meaning the frame reads empty;
  otherwise it falls back to the ISM. Codex can drop a prompt this way at a compact
  boundary. The clear sequence is not trusted to have emptied the composer.
  `before_agent_check` is required.
- A Grok rejection of `/compact` or `/clear` proves the CLI consumed the command,
  and no submit hook records that. The compaction sender records the submit
  itself (`record_composer_submit`). Otherwise the ledger keeps the command held,
  and the `/clear` resubmission would be a bare Enter into an empty composer.
- A ledger refusal logs `snapshot_source=ledger` with no frame fields and no
  draft length, because the ledger keeps a draft's provenance and not its text. The pane-frame
  refusal log remains only for the read-back after a failed Enter.
- A wake drains a cancelled staged `/compact`, which is daemon text, and then
  delivers. It no longer skips the wake.
- The watchdog's idle reprompt and stuck Enter read the ledger
  (`composer_refuses_automation`), not a frame, so a provider-limit block also holds
  them. A hold does not count as a failed delivery.
- The Claude transcript reader does not map `apiError: "usage_limit_reached"` to a
  terminal provider error. That mapping would terminalize interactive seats, which
  must survive the limit for the operator's continue line. The StopFailure
  classification and the ledger block cover the limit.
- The unknown-composer refusal and the usage-limit attention message name
  `release_composer`.
- The host computes `submit` without per-slot paste state. gclient sends a paste
  as `Paste`, never as `Input` wrapped in paste markers, and web input reaches
  the ledger as an operator write, not a frame. An `Input` submits on a CR or on
  an escape sequence that gterm's key parser reads as unmodified Enter, because a
  child that sets kitty flag 8 receives Enter as `\x1b[13;1u`. A `Paste` submits
  only when the child has bracketed paste off and the text holds such an Enter.
  A missed Enter would leave a draft until `release_composer`, so modified and
  released Enter are the only escape forms excluded.

## Not Recommended

- Reversing the 09-29 confirmed-empty policy (ruling 1a): rejected by Josh.
- Patching the pane parser further: Orchestrator ruling, 2026-10-08.
- Claude channels as the main path: research preview, development flag and consent
  dialog, and no `/compact`.
