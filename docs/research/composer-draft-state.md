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
   5. `thread/compact/start` on a TUI-attached thread writes the rollout `compacted`
      record that the existing boundary waiter reads.

Transcript turn state (`transcript_cursor.turn_settled`) remains the authority for
idle and turn completion, and the pane probe is removed from the wake gate and the
compaction sender.

## Not Recommended

- Reversing the 09-29 confirmed-empty policy (ruling 1a): rejected by Josh.
- Patching the pane parser further: Orchestrator ruling, 2026-10-08.
- Claude channels as the main path: research preview, development flag and consent
  dialog, and no `/compact`.
