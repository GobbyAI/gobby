# Live contract and polling

# Live mode

Select elements in the browser, pick a design action, and get AI-generated
HTML+CSS variants hot-swapped through the app's existing dev-server HMR.

## Prerequisites

A dev server with HMR (Vite, Next.js, Bun, etc.) must already be running, or a
static HTML file must already be open in the browser. Locate the existing app
URL from terminal output, documentation, or an open tab. This flow does not
start or stop the app server, allocate app ports, or change Gobby daemon state.

Resolve `<scripts_dir>` once by calling
`materialize_skill_scripts(name="impeccable")` on `gobby-skills`; it returns
the absolute path of the materialized `scripts/` tree. Export the returned
`environment.PUPPETEER_CACHE_DIR` before any browser-engine invocation. Keep
that same path for the full lifetime of every live helper process: returned
generations remain available until an explicit `gobby uninstall impeccable`. If
materialization or Node is unavailable, live mode cannot run; continue with
ordinary source edits and browser inspection.

The bundled scripts own their helper process and journal. Gobby provides no
daemon-level live-session or port manager.

## The contract (read once)

Execute in order. No step skipped, no step reordered. Every tool output in live mode may carry an `_instructions` field: it is the authoritative next step for that exact situation, with real ids and paths substituted; when it conflicts with your recollection of this document, `_instructions` wins.

1. `live.mjs`: boot. If the request names or implies a file, route, or app inside a monorepo, infer the concrete path and run `node <scripts_dir>/live.mjs --target <path>` instead; then run the rest of this live session from the returned `projectRoot`. The boot resolves the app root from dev-server config files and persists it in `.impeccable/live/roots.json`; every helper re-anchors to that manifest at startup (a wrong cwd cannot fork session state), and relative helper args like `--file` resolve against the app root. The helper may report legacy `PRODUCT.md` / `DESIGN.md` inputs; treat them as advisory because `.impeccable.md` is Gobby's design-context authority.
2. Open the app URL that serves `pageFile` (infer from `package.json`, docs, terminal output, or an open tab). Never use `serverPort`; it's the helper, not the app. **Cursor:** `browser_navigate` to that URL before polling; do not skip. **Other harnesses:** use the available browser tool; if the URL is uncertain, ask the user once.
3. Poll loop with the default long timeout (600000 ms). Run `live-poll.mjs` again immediately after every event or `--reply`; Codex runs this one-shot poll in the foreground. Never pass a short `--timeout=`. The global bar's **Impeccable mark** dims with a pulsing amber dot when nothing is polling `/poll`; restart `live-poll.mjs` to reconnect.
4. On `generate`: reuse `event.scaffold` when present; read the screenshot if present; load the action's reference; deliver variants; `--reply done`; poll again. Generate in this thread: you already hold the project's tokens and layout. The overlay preview IS the verification channel; do not screenshot, re-render, or QA variants between generate and accept. Load the craft floor by calling `get_skill_file(name="impeccable", path="references/craft-floor.md")` on `gobby-skills`; apply its contrast, spacing, and type floors by construction as you write, then run full verification once at accept on the chosen variant.
5. On `steer`: read the message and `pageUrl`; do the work; `--reply steer_done`; poll again. No pickup ack.
6. On `accept` / `discard`: the poll script runs `live-accept.mjs`, acknowledges delivery, and prints `_completionAck`. Plain accepts/discards are terminal immediately; carbonize accepts stay recoverable until `live-complete.mjs --id EVENT_ID` runs. Finish that cleanup before polling again.
7. If interrupted, run `live-status.mjs` or `live-resume.mjs` before guessing. The journal under `.impeccable/live/sessions/` is canonical and replays unacknowledged work after a helper restart; the injected `live.js` re-attaches when the page reopens. Fall back to the direct-edit loop only when `live-resume.mjs` reports no active session, never because disconnects felt frequent.
8. On `exit`: run the cleanup at the bottom.

Harness policy:
- **Claude Code**: run the poll as a **background task** (no short timeout); the harness notifies you on completion. Do not block the shell.
- **Cursor**: **one-shot** poll in a **background terminal** with notify on `"type":"(steer|generate|accept|discard|manual_edit_apply|variant_mount_failed|prefetch|exit)"`; handle, `--reply`, restart the poll. Do **not** use `--stream` on Cursor (measured ~5s pickup vs sub-second one-shot).
- **Codex**: default one-shot poll in a **yielded foreground exec session**. No `&`, no `--stream`, never leave Live without an active foreground poll. Starting the poll is not enough: SERVICE it (keep reading the exec session until it returns an event). Never announce "waiting for the user" and idle; a yielded poll nobody reads is a dead session, and the user's Go sits unanswered.
- **Other harnesses**: one-shot foreground unless you know stdout reliably returns when a shell exits.

Delivery policy: atomic single-edit delivery everywhere; do not switch a harness to progressive publishing unless its poll loop is known not to block on the extra calls.

Chat is overhead. No recap, no tutorial output, no pasting the
`.impeccable.md` body. Spend tokens on tools and edits; on failure, one or two
short sentences.

## Poll loop

```
LOOP:
  node <scripts_dir>/live-poll.mjs   # default long timeout; no --timeout=
  Read JSON; dispatch on "type"

  "generate"  → Handle Generate; reply done; LOOP
  "steer"     → Handle Steer; reply steer_done; LOOP
  "accept"    → Handle Accept; complete carbonize cleanup if required; LOOP
  "discard"   → Handle Discard; LOOP
  "prefetch"  → Handle Prefetch; LOOP
  "manual_edit_apply" → Handle Manual Edit Apply; reply done|partial|error; LOOP
  "variant_mount_failed" → Fix the variant files; reply done --file <path>; LOOP
  "timeout"   → LOOP
  "exit"      → break → Cleanup
```

`variant_mount_failed` means the browser could not render what you published (`variant`, module `url`, `error`). The user sees a persistent error card, not variants. Fix the variant files, then `--reply EVENT_ID done --file <manifest or source path>`; the browser retries on its own.

**Stream mode** (`--stream`, experimental, never on Cursor): one long-lived process, one JSON line per event, `--reply` from a separate command. Only for harnesses that read incremental stdout reliably.
