# Web Activity Panel Live Terminal

**Plan ID:** activity-panel-live-terminal

## O1: Overview

`kind: framing`

Add a live terminal view to the web UI activity panel — the herdr-style "watch any agent's pane, take control when needed" surface. xterm.js was previously removed (the standalone `TerminalsPage` was deleted in the provider-agnostic-resume work) and is not coming back; the renderer is **wterm** (`@wterm/dom`, DOM-rendered, Apache-2.0; the `@wterm/react` wrapper is explicitly not used — see C1 fact e) with the **libghostty VT core** (`@wterm/ghostty`, Ghostty v1.3.1, ~400 KB wasm fetched at runtime). DOM rendering provides native text selection, copy/paste, find, and accessibility. ghostty-web (Coder) was evaluated and rejected: it has a known wasm memory-corruption bug triggered by emoji-heavy output, which is exactly what Claude Code emits.

The daemon plumbing already exists and works end to end: raw-ANSI push streaming (`terminal_output` from the PTY bridge in `src/gobby/agents/tmux/pty_bridge.py` or `pipe-pane` FIFO), keystroke injection (`terminal_input` → PTY write or tmux send-keys, `src/gobby/servers/websocket/handlers/core.py:286`), resize (`tmux_resize` → `TIOCSWINSZ` + `refresh-client`), and per-connection bridge cleanup (`src/gobby/servers/websocket/tmux.py:67-79`). The orphaned client hook `web/src/hooks/useTmuxSessions.ts` implements this whole protocol and is revived, patched, and consumed by the new tab. The janky `SessionInteractionModal` "Capture Pane" mode (one-shot 80-line `capture_output` with a manual Refresh button) and the blind "Send Keys" mode are removed, superseded by the live terminal.

## C1: Constraints

`kind: framing`

- Frontend-only. **No daemon changes** — attach/detach/input/resize/list/broadcast and disconnect cleanup are confirmed working.
- New tab lives in the ActivityPanel (`web/src/components/activity/`), consistent with the direction that the activity panel is the canonical surface for session interaction. Not inside SessionsTab detail, not a standalone page.
- Input model: the rendered terminal is **always read-only display** — the user can never type into it on any device. This mirrors the existing web attach flow (chat-style input driving tmux via send_keys) and sidesteps wterm's broken iOS hidden-textarea input entirely. Read-only is enforced by neutralizing wterm's hidden input textarea after init and overriding the container's textbox ARIA (see 2.2 and the resolved facts below) — a no-op `onData` is **not** sufficient. `onData` is always supplied, but solely as a VT protocol-response relay (with user input neutralized it can never fire for keystrokes). "Take control" reveals the composer bar (2.3) on all devices; every keystroke reaches tmux only through the existing `terminal_input` daemon path (PTY write, or tmux send-keys fallback).
- Gobby runs agent sessions and interactive sessions on **separate tmux sockets**. Session identity is socket-qualified end to end — `{name, socket}` / `sessionKey` in hook attach state, picker selection, attach-state comparisons, and vanished-session detection. Never assume a default socket; two sockets can host the same session name. The daemon's list handler (`_handle_tmux_list_sessions`, `src/gobby/servers/websocket/tmux.py:190-300`) emits `socket: "default" | "gobby"` per row, but both of its identity fields are cross-socket unsafe: `gobby_session_id` comes from a pane map keyed by **bare, server-local pane IDs** (`%N` — the same ID can exist on both tmux servers) applied to rows from both sockets, and `agent_run_id` is matched by **session name alone** across both sockets. Under collisions, a gobby row can carry a wrong non-null `gobby_session_id` and a default row a wrong `agent_run_id`. Worse: the pane map is built from **every** active/paused Gobby session — including spawned-agent sessions, whose `terminal_context.tmux_pane` is backfilled with a **gobby-socket** pane ID — so under a `%N` collision even a default row's `gobby_session_id` can name an agent-backed session, and the name-only loop makes `agent_managed` equally unsafe on default rows. Identity trust is therefore socket-specific **and provenance-checked**: `socket === "default"` rows join by `gobby_session_id` **and only into non-agent-backed sessions** (matched `GobbySession.agent_run_id == null` — the frontend session type carries this field); `socket === "gobby"` rows join by non-null `agent_run_id` only; the raw `agent_managed`/`agent_run_id` fields on default rows are ignored entirely, and the agent badge derives solely from a trusted gobby-row join (see 2.1).
- Resolved upstream facts (verified against `vercel-labs/wterm` 0.3.0 source — no implementation-time unknowns remain): (a) `WTerm.init()` **unconditionally** creates a hidden input textarea, focuses it, refocuses it on container click, and `preventDefault()`s all keydown; no `readOnly`/`disableStdin` option exists. With `onData` unset, keystrokes are echoed locally into the VT core (`write(data)` fallback), so `onData` must always be supplied — but a no-op alone still leaves keyboard capture and a focus trap. Read-only requires neutralizing the textarea directly (disable, remove from tab order, blur — a disabled textarea receives no key/input/paste events and cannot be focused, so wterm's init/click `focus()` calls become no-ops); see 2.2. (b) `@wterm/ghostty`'s `exports` map exposes only the root entry; the `wasm/` directory ships in `files` but is **not** an exported subpath, so Vite `?url` bare-specifier imports cannot reach it — the wasm is served via a postinstall copy to `public/` and `GhosttyCore.load({ wasmPath })` (see 1.1). (c) `@wterm/dom`'s `WTerm` constructor options accept `onData`, `onTitle`, and `onResize(cols, rows)` — **cols first**; the core has no readiness callback — readiness is `wt.init()` resolving, after which dimensions are read from `wt.rows`/`wt.cols` (see 2.2). (d) `@wterm/dom` ships its required stylesheet (layout, row sizing, scrolling, selection, cursor, ANSI palette variables) via the `./css` export — `import "@wterm/dom/css"` is mandatory (see 2.2). (e) `@wterm/react`'s `<Terminal>` is **unusable in this app**: it initializes and destroys `WTerm` inside a React 19 callback-ref cleanup return (its source says so verbatim), which React 18.3.1 — this app's runtime, mounted under `<StrictMode>` in `web/src/main.tsx` — ignores; the detach path calls the ref with `null`, whose branch early-returns without destroying, so `wt.destroy()` never runs and every keyed remount, fallback swap, Retry, and unmount leaks a live `WTerm` (ResizeObserver, listeners, pending init work). It also hardcodes `aria-multiline="true"`, which is invalid alongside `role="log"`. The wrapper is therefore not a dependency; `TerminalView` drives `WTerm` directly (see 2.2). (f) VT protocol replies (`bridge.getResponse()`) surface only through `onData`. (g) `GhosttyCore.init()` overwrites its terminal pointer without freeing the prior one, and `WTerm.destroy()` never deinitializes the core — a core instance must never be cached or shared across renderer instantiations (see 1.1). (h) `WTerm.destroy()` sets `_destroyed` and then wipes `element.innerHTML` — it clears whatever currently lives in the host element, including a successor instance's DOM — and never nulls `bridge` (a post-destroy `resize()` would drive a deinitialized core). `WTerm.init()` re-checks `_destroyed` immediately after its awaited core load and returns before any DOM/renderer/input setup, so destroying an instance synchronously while `init()` is still pending is safe. `init()`'s **rejection** path, however, calls `this.destroy()` internally with no `_destroyed` guard — a second destroy outside the embedder's control that can fire long after cleanup. Consequences: wrapper cleanup must destroy immediately and synchronously (never deferred behind the init promise — promise handlers run after StrictMode's synchronous setup→cleanup→setup replay, so a deferred destroy fires after the replacement has mounted), and each instance must mount in its own wrapper-owned child node so any late internal destroy wipes only a detached node (see 2.2).
- ghostty core everywhere, lazy-loaded on first tab open. No device-split cores. wterm's built-in ~12 KB core (already inside `@wterm/dom`) is the automatic fallback only when `GhosttyCore.load()` fails. Never render a blank pane.
- Design work goes through the gobby-skills design system, not a raw file read: every UI-authoring deliverable loads `get_skill(name="impeccable")` on gobby-skills before writing JSX/CSS (its context protocol consumes `.impeccable.md`), plus the relevant reference docs named per-deliverable via `get_skill_file(name="impeccable", path=...)`. Binding constraints: deutan-safe state colors (no red/green pairing for view-vs-control), WCAG 2.2 AA contrast, existing `chip` / `btn btn-accent btn-sm` idioms.
- Non-test TS/TSX files stay under 1,000 lines.
- No backward compatibility — 0.5.0 is unshipped.
- Known accepted limitations: every attached web viewer is a separate tmux client and tmux sizes the session to the smallest client, so multiple simultaneous viewers shrink each other; and because each viewer relays VT protocol replies through `terminal_input` (2.2/2.4), multiple viewers can send tmux duplicate replies to the same device query. Nothing is built for either in v1.
- The daemon's `capture_output` MCP tool is untouched (still used by agents/CLI).

## P1: Rendering Foundation

`kind: framing`

**Goal**: wterm dependencies, the per-instantiation ghostty core loader, and the revived WS hook are in place and tested before any UI exists.

### 1.1 Add wterm dependencies and ghostty core loader [category: code]

`kind: deliverable`

Targets: `web/package.json`, `web/package-lock.json`, `web/scripts/copy-ghostty-wasm.cjs`, `web/src/lib/ghosttyCore.ts`, `web/vite.config.ts`

Add runtime deps to `web/package.json`: `@wterm/dom`, `@wterm/ghostty` (latest published; `web/package-lock.json` is regenerated and committed in the same change). `@wterm/react` is **not** added (C1 fact e — its React-19-only ref-cleanup lifecycle leaks under this app's React 18.3.1).

Wasm serving (decided — see C1 resolved facts; the `exports` map blocks subpath imports): new `web/scripts/copy-ghostty-wasm.cjs`, mirroring the existing VAD asset-copy postinstall script, copies `node_modules/@wterm/ghostty/wasm/ghostty-vt.wasm` → `web/public/wasm/ghostty-vt.wasm`; wire it into the existing `postinstall` script chain in `web/package.json`, and add `public/wasm/` to `web/.gitignore` (build artifact, recreated on install). The public asset is served identically by the Vite dev server and the production build.

Create `web/src/lib/ghosttyCore.ts` — a thin per-call loader. **Never cache or share a `GhosttyCore` instance** (C1 resolved fact g: `GhosttyCore.init()` overwrites its terminal pointer without freeing, and `WTerm.destroy()` never deinitializes the core — a shared instance leaks a wasm terminal allocation per keyed remount and corrupts state across concurrent renderers). Every call returns a fresh core; repeat loads are cheap because the browser HTTP-caches the wasm bytes:

```ts
import { GhosttyCore } from "@wterm/ghostty"

const WASM_PATH = "/wasm/ghostty-vt.wasm"

export function loadGhosttyCore(): Promise<GhosttyCore> {
  return GhosttyCore.load({ wasmPath: WASM_PATH })
}
```

In `web/vite.config.ts` manualChunks (:66-76): delete the dead `vendor-xterm` rule (:70) and add `if (id.includes("@wterm")) return "vendor-wterm"`.

**Acceptance:**

- 1.1.1 - `@wterm/dom` and `@wterm/ghostty` are dependencies (and `@wterm/react` is not) with the lockfile updated in the same change. file: `web/package.json`.
- 1.1.2 - Postinstall copy script places the ghostty wasm under `public/wasm/` and runs from the package `postinstall` chain. file: `web/scripts/copy-ghostty-wasm.cjs`.
- 1.1.3 - Loader returns a fresh `GhosttyCore` per call via `wasmPath` — no module-level instance or promise cache. file: `web/src/lib/ghosttyCore.ts`.
- 1.1.4 - Distinct core instances across consecutive calls, and a successful load after a rejected one, are pinned. test: `web/src/lib/__tests__/ghosttyCore.test.ts::fresh core per call`.
- 1.1.5 - `vendor-xterm` chunk rule is gone, `@wterm` maps to `vendor-wterm`, and `npm run build` succeeds with the wasm asset present in the output. file: `web/vite.config.ts`.

### 1.2 Revive useTmuxSessions with connection-state patches [category: code]

`kind: deliverable`

Target: `web/src/hooks/useTmuxSessions.ts`

The hook is currently orphaned (zero importers) but complete: own WebSocket to `/ws`, subscribes `['terminal_output','tmux_session_event','session_event']`, request/response attach/detach/create/kill/list, `sendInput` (`terminal_input`), `resizeTerminal` (`tmux_resize`), `onOutput(cb)`, 2 s reconnect, visibilitychange refresh. It stays on its own socket — the shared `useWebSocketEvent` singleton is receive-only, and a dedicated socket whose lifetime equals the tab's lifetime gets free daemon-side PTY-bridge cleanup on disconnect.

Patches (~80 lines):

1. Expose `connected: boolean` in `TmuxSessionsResult`, set true in `onopen`, false in `onclose`.
2. In `ws.onclose`, clear the attached target and `streamingId` — the server-side bridge dies with the socket; current code leaves stale attach state across the reconnect. (Consequence: vanished-during-reconnect detection cannot compare against `attachedTarget`, which is already null when the post-reconnect list arrives — TerminalTab retains the last-attached key itself; see 2.4.)
3. Socket-qualified attach identity: replace the name-only `attachedSession` with `attachedTarget: {name, socket} | null` (or an opaque `sessionKey`), used for attach-state comparisons and vanished-session detection. Two sockets can host the same session name; name-only comparison can leave the old pane attached and route input to the wrong terminal.
4. Expose `sessionsLoaded: boolean` — false initially, true on the first `tmux_sessions_list`, reset to false on socket close. The initial empty array is otherwise indistinguishable from an authoritative empty response.
5. Reconnect guard via **connection generations** — a boolean `shouldReconnect` ref is unsafe under this app's `<StrictMode>` (`web/src/main.tsx`): the dev effect replay's cleanup would flip it false (killing future real reconnects) or the replayed setup would flip it back true (letting a delayed `onclose` from the disposed first socket schedule a zombie). Follow the existing `connectionGeneration` pattern in `web/src/hooks/useWebSocketEvent.ts`: each connect increments a generation counter and captures its value; `onopen`/`onclose`/`onerror` and the delayed 2 s reconnect callback all no-op when their captured generation is stale or their socket is not the current one; unmount cleanup increments the generation and closes the current socket. A replaced connection's late events can then never schedule work, while the live connection reconnects normally.
6. Request-error correlation: the daemon replies `{type: "error", request_id, ...}` for missing sessions and bridge failures; the hook currently ignores `error` messages, leaving pending attach/detach state stuck. Handle `error` frames by `request_id`: clear the pending attach/detach and any loading flag, and expose `attachError: string | null` plus an explicit `clearAttachError()`. The error is also cleared whenever a new attach/detach request is actually issued; the explicit operation exists because the tab's state machine halts while `attachError` is set (2.4) and needs a non-circular way to re-arm — Retry and selection changes call it.
7. Pending-request guard: expose `requestPending: boolean`, true from an attach/detach send until its correlated result or error frame arrives; `attachSession`/`detachSession` are no-ops while a request is pending, so effect re-runs can never enqueue duplicate sends. Each request carries a token tied to its connection; **on socket close the token is invalidated and `requestPending` (plus any error state belonging to the in-flight operation) is cleared synchronously** — the correlated reply died with the socket, and without this the post-reconnect state machine is permanently wedged behind a pending flag that can never resolve. A late frame matching a stale token is ignored.
8. Document the single-consumer constraint on `onOutput` (one callback ref by design; TerminalTab is the sole consumer).

**Acceptance:**

- 1.2.1 - Hook exposes `connected` and `sessionsLoaded`, tracks the attached target socket-qualified, and clears attach state on socket close. file: `web/src/hooks/useTmuxSessions.ts`.
- 1.2.2 - Open/close/attach/clear-on-close and the generation-based reconnect guard are pinned with a fake WebSocket and advanced timers: a StrictMode-style effect replay (setup → cleanup → setup) yields exactly one live socket; an unexpected close of that live socket schedules exactly one reconnect; a delayed `onclose` from the disposed first socket schedules nothing; zero reconnects after final unmount. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::reconnect generation guard`.
- 1.2.3 - The same session name on two different sockets is treated as two identities: switching triggers detach-then-attach and input follows the new `streamingId`. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::socket qualified identity`.
- 1.2.4 - Exact `terminal_input` and `tmux_resize` wire payloads are asserted against the current `streamingId`, including no-send guards when disconnected or unattached. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::wire payloads`.
- 1.2.5 - Daemon `error` frames correlated by `request_id` clear pending attach/detach state and surface `attachError`; `clearAttachError()` re-arms; attach-failure and detach-failure paths are pinned each including a successful retry after the error. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::attach error handling`.
- 1.2.6 - While an attach or detach request is pending, repeated `attachSession`/`detachSession` calls send nothing (`requestPending` gating). test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::duplicate send suppression`.
- 1.2.7 - A socket close during an in-flight attach and during an in-flight detach clears `requestPending` synchronously, a new request succeeds after reconnect, and a late result/error frame carrying the stale request token is ignored. test: `web/src/hooks/__tests__/useTmuxSessions.test.ts::close clears pending request`.

## P2: Terminal Tab

`kind: framing`

**Goal**: A working `terminal` tab in the ActivityPanel: pick a tmux session, watch its pane live, take control to type (desktop) or use the composer (touch).

### 2.1 Terminal session join helpers [category: code]

`kind: deliverable`

Target: `web/src/components/activity/terminal/terminalSessions.ts`

Pure helpers (~80 lines), no React. `TmuxSession` rows from the WS list (`name`, `socket`, `gobby_session_id`, `agent_run_id`, `pane_dead`, `agent_managed`, `session_title`) are joined with the `GobbySession[]` prop already flowing into ActivityPanel (the `GobbySession` type). Note `GobbySession` does **not** carry tmux name/socket — attach coordinates always come from the WS list.

**Socket-specific identity join with provenance check** (required by the daemon's list-handler behavior, see C1: both identity fields are cross-socket unsafe — the pane map uses bare server-local pane IDs across both sockets and includes spawned-agent sessions, and `agent_run_id` matches by name alone across both sockets): rows with `socket === "default"` join **solely** on `TmuxSession.gobby_session_id === GobbySession.id` **and** `GobbySession.agent_run_id == null` — a matched agent-backed session on a default row can only be a `%N`-collision artifact (agent panes live on the gobby socket), so it is rejected and the row stays external; rows with `socket === "gobby"` join **solely** on non-null `TmuxSession.agent_run_id === GobbySession.agent_run_id`. The other identity field is ignored per socket, and the daemon's raw `agent_managed`/`agent_run_id` values on default rows are never trusted for anything: the `agentManaged` badge is true only for gobby-socket rows whose `agent_run_id` join succeeded. Anything weaker either renders every spawned agent — the central "watch any agent" case — as external, or cross-joins the wrong session under pane-ID/name collisions.

- `sessionKey(t: TmuxSession): string` → `` `${t.socket}:${t.name}` `` (stable selection key).
- `joinTmuxSessions(tmux: TmuxSession[], gobby: GobbySession[] | undefined): JoinedTerminalSession[]` — each entry carries the TmuxSession, the matched GobbySession or null (via its socket's identity field), a display label (Gobby `#seq_num` + title when matched, raw tmux `name` otherwise), and badge flags: `dead` (`pane_dead`), `agentManaged` (trusted gobby-row join only — never the raw `agent_managed` field), `external` (no trusted Gobby match).
- `findByGobbySessionId(joined: JoinedTerminalSession[], sessionId: string): JoinedTerminalSession | null` — focus resolution over the **joined** result, so a jump targets gobby-socket agent rows matched via `agent_run_id` too. TerminalTab's `focusSessionId` consumption (2.4) uses this helper, never a raw `gobby_session_id` scan.

**Acceptance:**

- 2.1.1 - Socket-specific join (default → `gobby_session_id` restricted to sessions with `agent_run_id == null`, gobby → `agent_run_id`, other field ignored per socket) plus trusted-only `agentManaged` derivation, labels, badges, key derivation, and focus lookup are implemented as pure functions. file: `web/src/components/activity/terminal/terminalSessions.ts`.
- 2.1.2 - Matching, unmatched-external, and dead-pane cases are pinned. test: `web/src/components/activity/terminal/__tests__/terminalSessions.test.ts::joins tmux and gobby sessions`.
- 2.1.3 - Collision cases are pinned: identical session names on both sockets stay distinct entries; a gobby row carrying a wrong non-null `gobby_session_id` and a default row carrying a wrong `agent_run_id` do not cross-join; a default row whose `gobby_session_id` and `agent_managed: true` point at a colliding **agent-backed** session (matched `GobbySession.agent_run_id != null`) does not join and renders no agent badge; a gobby-socket agent row joins via `agent_run_id`, is not marked external, and `findByGobbySessionId` resolves to the correct socket's row under all collisions. test: `web/src/components/activity/terminal/__tests__/terminalSessions.test.ts::socket specific identity join`.

### 2.2 TerminalView wterm wrapper [category: code] (depends: 1.1)

`kind: deliverable`

Target: `web/src/components/activity/terminal/TerminalView.tsx`

Drives `@wterm/dom`'s `WTerm` directly (~220 lines) as a **pure display surface** — the user can never type into it. `@wterm/react` is **not used** (C1 fact e: its React-19 callback-ref-cleanup lifecycle never destroys under this app's React 18.3.1 + StrictMode). Props: `onSizeChange?: (rows: number, cols: number) => void`, `onReady?: (rows: number, cols: number) => void`, `onProtocolResponse?: (data: string) => void`. Exposes `write(data: string)` and `getSize(): {rows, cols} | null` via `useImperativeHandle`, backed by the current `WTerm` instance ref plus internally tracked dimensions.

Before styling the fidelity chip and error card, load `get_skill(name="impeccable")` on gobby-skills and its `harden` reference doc via `get_skill_file` (edge-state and error-surface patterns).

- **Stylesheet**: `import "@wterm/dom/css"` at the top of this lazily loaded module (C1 fact d — layout, row sizing, scrolling, selection, cursor, and ANSI palette variables all live there; the DOM renderer is nonfunctional without it). Gobby theme overrides layer **after** the import.
- **Container ARIA**: TerminalView renders its own container div — there are no wrapper defaults to fight: `role="log"`, `aria-label="Terminal output (read-only)"`, and **no `aria-multiline` or any other textbox-only attribute** (`aria-multiline` is unsupported on `role="log"`; the dropped wrapper hardcoded it — C1 fact e). ARIA lives on this outer container only; each `WTerm` instance mounts into its own child node inside it (see Instance lifecycle).
- **Instance lifecycle** (the core correctness mechanism): one layout effect keyed on the container element and the resolved core appends a fresh **per-instance mount node** (a plain child div, `height: 100%`) to the ARIA container, creates `new WTerm(mountEl, { core?, autoResize: true, onData, onResize })`, stores it in a ref, and calls `wt.init()`. Each instance carries a `disposed` flag. Effect cleanup is **synchronous, immediate, and exactly once — even while `init()` is still pending**: set `disposed`, cancel any pending resize-debounce timer, call `wt.destroy()` directly, remove the instance's mount node, null the ref. Destroy is **never** deferred behind the init promise: StrictMode replays setup→cleanup→setup synchronously while promise handlers run later, so a deferred destroy would fire after the replacement mounted and — `destroy()` wipes `element.innerHTML` (C1 fact h) — erase the live replacement's DOM. Immediate destroy mid-init is safe because `init()` re-checks `_destroyed` after its awaited core load (C1 fact h). The per-instance mount node contains the one upstream path the wrapper cannot intercept — a pending `init()` that **rejects** after cleanup calls `destroy()` internally a second time (C1 fact h) — so any late internal destroy wipes only the dead instance's already-detached node, never a successor. Every async continuation (the `init()` ready path, core-load resolution) checks `disposed` first — stale `onReady`, neutralization, or fallback work from a replaced instance never fires. Under React 18 StrictMode's dev effect replay this yields create→destroy→create with exactly one live instance and the survivor's DOM, callbacks, and resize state intact.
- **Core loading**: calls `loadGhosttyCore()` per instantiation — a fresh core every time, never shared (C1 fact g / 1.1); on success the effect constructs `WTerm` with `{core}`; on failure it falls back to the built-in core (no `core` option) and shows a small "reduced fidelity" chip with a Retry action — retry state is local to TerminalView: clear the failure flag and call `loadGhosttyCore()` again for a brand-new core (the effect re-runs, destroying the fallback instance). If the fallback also errors, render an inline error card — never a blank pane.
- **Read-only enforcement** (C1 fact a — wterm unconditionally creates, focuses, and click-refocuses a hidden input textarea and `preventDefault()`s keys; a no-op `onData` is not sufficient): in every instance's post-`init()` ready path (guarded by `disposed`), neutralize the textarea — `const ta = wt.element.querySelector("textarea"); ta.disabled = true; ta.tabIndex = -1; ta.blur()`. A disabled textarea receives no key, input, or paste events and cannot take focus, so wterm's init/click `focus()` calls become no-ops, Tab skips the surface, and no keyboard trap exists. Native DOM text selection and wheel scrollback are unaffected.
- **Protocol responses**: `onData` is always supplied in the `WTerm` options — never omitted (C1 fact a: an unset `onData` echoes keystrokes locally into the VT core). With user input neutralized it can only fire for VT protocol replies (C1 fact f); forward them to `onProtocolResponse` (TerminalTab routes them to `sendInput` so tmux's device queries get answered; multi-viewer duplicate replies are an accepted C1 limitation).
- **Ready handshake with synchronous size handoff**: readiness is `wt.init()` resolving (C1 fact c — the core has no ready callback) — read `wt.rows`/`wt.cols` off the instance and fire the wrapper's `onReady(rows, cols)` so the parent can sequence the post-attach repaint without waiting on the debounced resize path (the daemon starts the PTY reader before `tmux_attach_result` returns, so frames can arrive before the keyed view has committed). `onReady` fires per instantiation: initial ghostty mount, the built-in-core fallback swap, and every Retry remount each fire their own `onReady` (this is also where read-only neutralization runs), and the parent re-triggers the repaint for each — a renderer that appears mid-stream starts blank until refreshed.
- **Resize**: `autoResize: true` in the options; the core emits `onResize(cols, rows)` — **cols first** (C1 fact c); the adapter transposes into `onSizeChange(rows, cols)` behind a 200 ms trailing-edge debounce. The debounce timer is cancelled in effect cleanup (Instance lifecycle) — a trailing fire after destroy would report a dead instance's dimensions and trigger a stale `tmux_resize`. Current dimensions are tracked internally and exposed synchronously via `getSize()` (server PTY defaults to 200×50; without an immediate correctly-sized resize the first repaint mis-wraps).
- Container carries `data-testid="terminal-view"` for Playwright.

**Acceptance:**

- 2.2.1 - View drives `WTerm` from `@wterm/dom` directly with a fresh ghostty core per instantiation, imports `@wterm/dom/css` with Gobby overrides layered after it, falls back to the built-in core with fidelity chip and Retry, and renders an error card as last resort. file: `web/src/components/activity/terminal/TerminalView.tsx`.
- 2.2.2 - Read-only enforcement and ARIA are pinned with a mocked `@wterm/dom`: on ready the textarea is disabled, untabbable, and blurred; the container's final attribute set is `role="log"` plus `aria-label` with **no `aria-multiline`** or other textbox-only state; mount, click, Tab, and keyboard produce no output and move no focus into the surface; text selection still works; a synthetic protocol reply flows to `onProtocolResponse`; `onReady(rows, cols)` fires per instantiation (initial, fallback swap, Retry remount) with dimensions read off the instance. test: `web/src/components/activity/terminal/__tests__/TerminalView.test.tsx::read only and ready handshake`.
- 2.2.3 - The `onResize(cols, rows)` → `onSizeChange(rows, cols)` transposition, the 200 ms debounce, and `getSize()` are pinned with non-square dimensions (e.g. 211×57) so any transposition regression fails. test: `web/src/components/activity/terminal/__tests__/TerminalView.test.tsx::resize transposition`.
- 2.2.4 - Wrapper cleanup calls `destroy()` exactly once per created instance, **synchronously and immediately — including while `init()` is still pending** — and stale async work is suppressed, across initial mount, StrictMode double-mount, fallback swap, Retry remount, keyed replacement, and unmount. Pinned specifically: after a same-container StrictMode replay (and after a fallback replacement), the first instance's init is settled **late** and the second instance's DOM, callbacks, and resize state are asserted intact — no `innerHTML` wipe, no stale `onReady`; the first instance's init is **rejected** late and the replacement's DOM is asserted to survive upstream's internal error-path `destroy()` (per-instance mount node); a pending resize debounce is cancelled by cleanup — no post-destroy `onSizeChange` fires. test: `web/src/components/activity/terminal/__tests__/TerminalView.test.tsx::lifecycle destroy`.

### 2.3 Session picker and touch composer bar [category: code] (depends: 2.1)

`kind: deliverable`

Targets: `web/src/components/activity/terminal/TerminalSessionPicker.tsx`, `web/src/components/activity/terminal/TerminalKeysBar.tsx`, `.impeccable.md`

Before styling either component, load `get_skill(name="impeccable")` on gobby-skills (its context protocol consumes `.impeccable.md`) and its `interaction-design` reference doc via `get_skill_file` — form, focus, and touch-target patterns for the picker and composer.

- `TerminalSessionPicker` (~120 lines): Radix select (existing dependency) over `JoinedTerminalSession[]`, rendering label plus dead/agent-managed/external badges (deutan-safe, never color-only). Matched entries additionally reuse the existing `ActivityRowStatusDot` component for the Gobby session's lifecycle status, keeping its text label for non-color identification; unmatched external tmux rows stay on the badge path. Controlled: `value: string | null` (session key), `onChange`.
- `TerminalKeysBar` (~90 lines): **the control surface on all devices** — the same pattern as the existing web attach flow (chat-style input driving tmux). A plain DOM `<input>` (native soft-keyboard behavior on mobile for free) with a Send button, and quick-key chips with **exact pinned emissions**: Send → composed text + `"\r"`; Esc → `"\x1b"`; Tab → `"\t"`; Enter → `"\r"`; ↑ → `"\x1b[A"`; ↓ → `"\x1b[B"`; Ctrl+C → `"\x03"`; prompt chips 1/2/3 → the bare digit `"1"`/`"2"`/`"3"` with **no** carriage return (TUI prompt selectors act on the digit itself; the Enter chip covers flows that need explicit confirmation). All emissions go through a `sendInput(data: string)` prop — the existing `terminal_input` daemon path (PTY write, or tmux send-keys fallback). The terminal itself never captures keyboard input, so there is no keyboard trap and no reliance on wterm's input handling anywhere.

**Acceptance:**

- 2.3.1 - Picker renders joined sessions with badges and controlled selection. file: `web/src/components/activity/terminal/TerminalSessionPicker.tsx`.
- 2.3.2 - Keys bar sends composed text (+ `"\r"`) and every quick key's exact pinned byte sequence through `sendInput`, including bare-digit 1/2/3. test: `web/src/components/activity/terminal/__tests__/TerminalKeysBar.test.tsx::exact key emissions`.
- 2.3.3 - Matched picker entries render the reused `ActivityRowStatusDot` with its text label; unmatched entries do not. test: `web/src/components/activity/terminal/__tests__/TerminalSessionPicker.test.tsx::picker status dot`.

### 2.4 TerminalTab root with attach lifecycle [category: code] (depends: 1.2, 2.2, 2.3)

`kind: deliverable`

Targets: `web/src/components/activity/terminal/TerminalTab.tsx`, `.impeccable.md`

Tab root (~250 lines). Props: `sessions?: GobbySession[]`, `focusSessionId?: string | null`, `onFocusHandled?: () => void`. Owns the single `useTmuxSessions()` instance, selection state (`selectedKey`), and `controlMode: boolean`.

**Attach state machine** (one effect over hook state): when `connected && sessionsLoaded && !requestPending && attachError == null && selected && selected row present in the current list && attachedTarget key !== selected key` (socket-qualified comparison — never name-only) → if `streamingId != null` call `detachSession()`, else `attachSession(name, socket)`. The `requestPending` gate (1.2 patch 7) means rerenders can never enqueue duplicate attach or detach sends; the `sessionsLoaded` + row-presence gates matter on reconnect: `connected` flips true before the fresh `tmux_sessions_list` arrives, and a stale selection must never be re-attached until the new list confirms the row still exists (if it vanished, flow into the `sessionEnded` state instead). When the hook reports `attachError`, the machine halts and the error state renders with Retry; Retry calls `clearAttachError()` (1.2 patch 6) and the re-armed effect then issues exactly one appropriate attach or detach; selection changes also call `clearAttachError()` before the machine evaluates the new target, so a failed session never wedges switching. Attach only fires after `streamingId` clears, so the detach-result race in the hook cannot trigger. Output routing: `onOutput((runId, data) => { if (runId === streamingIdRef.current) viewRef.current?.write(data) })` — the server broadcasts every run's output globally; this filter is mandatory. Protocol replies: `<TerminalView onProtocolResponse={sendInput}>` (2.2) so tmux device queries get answered; the hook's no-send guards make this inert while unattached. Render `<TerminalView key={streamingId}>` so each attach starts a fresh screen.

**Vanished-session detection** (the hook clears `attachedTarget` on socket close — 1.2 patch 2 — so it cannot be the comparison source): TerminalTab keeps a `lastAttachedKeyRef` — the socket-qualified key of the most recent successful attach, surviving socket close, cleared on explicit user session switches. Whenever `sessionsLoaded` flips true (initial load and every reconnect), resolve staleness for **both** `lastAttachedKeyRef` **and** the current `selectedKey`: if either is set and its row is absent from the joined list, enter `sessionEnded` instead of auto-selecting or re-attaching — the `selectedKey` check matters when a selection never reached a successful attach (e.g. the socket closed mid-attach), where `lastAttachedKeyRef` is still empty and the stale selection would otherwise leave the state machine silently halted on its row-presence gate with no rendered state. Dismiss clears the vanished selection and the ref, then normal fallback auto-selection proceeds.

**Post-attach repaint handshake**: the daemon starts the PTY reader before `tmux_attach_result` returns, so early frames can be dropped or written into a view that immediately remounts. Keep the "attaching" state until both the attach result AND the keyed view's `onReady(rows, cols)` have fired, then send one immediate (non-debounced) `resizeTerminal(rows, cols)` using the dimensions delivered by `onReady` (or `viewRef.getSize()`) — never the debounced path — and the daemon's resize handler runs `refresh-client`, repainting the full screen into the now-ready renderer (`refreshTerminal` is the no-dimensions fallback). Because `onReady` fires per renderer instantiation, a fallback-core swap or Retry remount while attached re-triggers the same repaint sequence so the replacement renderer is never left blank. Effect cleanup detaches best-effort; the daemon's per-connection cleanup is the backstop. Tab switch unmounts the component (ActivityPanel's `tabContent()` switch) — detach + socket close is the intended on-demand model; re-attach repaints instantly.

**Control mode**: `controlMode` only toggles the `TerminalKeysBar` composer's visibility (all devices — the terminal itself is always read-only display and never takes keyboard focus, so there is no passthrough to gate and no keyboard trap). Resets to `false` on session switch and re-attach. Header shows a persistent "Viewing" / "Composer open" chip plus an accent border on the terminal container while the composer is open. Before styling the states, chips, and empty/ended/reconnecting copy, load `get_skill(name="impeccable")` on gobby-skills plus its `harden` and `ux-writing` reference docs via `get_skill_file` (edge-state surfaces; empty states that teach).

**States** (all gated on the hook's `sessionsLoaded` — before the first `tmux_sessions_list` arrives, show a loading state, never the empty state): no tmux sessions → empty state; sessions, none selected → auto-select pending `focusSessionId` match, else the first non-dead row, else the **first row** (an all-dead list still selects and renders its dead pane — the tab never sits selection-less while rows exist); attaching (until attach result + view ready) → spinner; `connected === false` → "Reconnecting" overlay; attached or selected session vanished (per the stale-key detection above) → `sessionEnded` notice + Dismiss; `pane_dead` → attach allowed (tmux renders the dead pane), composer disabled with tooltip. `focusSessionId` consumption: only once `sessionsLoaded` is true — resolve via `findByGobbySessionId` over the joined list (2.1), so agent rows matched through the `agent_run_id` fallback are found; else show a transient "No live terminal for this session" notice; either way call `onFocusHandled()`. A jump request must never be consumed or rejected while the list is still loading.

**Acceptance:**

- 2.4.1 - TerminalTab implements socket-qualified selection, the detach-then-attach state machine with `requestPending` and `attachError` gating, streaming-id output filtering, protocol-reply forwarding to `sendInput`, the ready-handshake repaint sequencing, stale-key vanished detection (both `lastAttachedKeyRef` and `selectedKey`), `sessionsLoaded` gating, and all listed states. file: `web/src/components/activity/terminal/TerminalTab.tsx`.
- 2.4.2 - Auto-select (including the all-dead-list fallback to the first row with the dead-pane view shown and composer disabled), attach args, session-switch sequencing, output filtering, focus-prop consumption (including deferred resolution until the list loads and a jump selecting a gobby-socket agent row via its `agent_run_id` join), and reconnect/ended states are pinned with a mocked hook and stubbed view. test: `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx::attach lifecycle`.
- 2.4.3 - The attaching state clears only after both attach result and view readiness, followed by exactly one immediate resize using the `onReady` dimensions — pinned with a non-square fixture asserting the `tmux_resize` payload preserves rows and cols; fallback-swap and Retry remounts re-trigger the repaint. test: `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx::ready handshake repaint`.
- 2.4.4 - Control mode toggles the composer; typed input never reaches the wire from the terminal surface itself, while a synthetic protocol reply does flow through `sendInput`. test: `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx::composer only input`.
- 2.4.5 - Attach failure halts the machine and Retry (via `clearAttachError`) issues exactly one new request; duplicate attach/detach sends are suppressed while a request is pending; a selected session vanishing during reconnect flows to `sessionEnded` (via `lastAttachedKeyRef` against the first post-reconnect list, with a duplicate-name row on the other socket present to prove socket-qualified detection) and Dismiss re-enables fallback selection; a socket close **during an in-flight attach** is pinned for both target outcomes — target row present after reconnect → the machine issues exactly one fresh attach (1.2.7's cleared `requestPending`), target row absent → `sessionEnded` renders even though `lastAttachedKeyRef` was never set. test: `web/src/components/activity/terminal/__tests__/TerminalTab.test.tsx::attach error and reconnect gating`.

### 2.5 Register the terminal tab in the ActivityPanel [category: code] (depends: 2.4)

`kind: deliverable`

Targets: `web/src/components/activity/ActivityPanelTabs.tsx`, `web/src/components/activity/ActivityPanel.tsx`

- `ActivityPanelTabs.tsx`: add `"terminal"` to the `ActivityTab` union (:3-19); insert `{id: "terminal", label: "Terminal", icon: …}` into `ACTIVITY_PANEL_TABS` immediately after `sessions` (its operational sibling). Icon: prompt glyph in the existing stroke idiom — `<polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" />`. The activity-panel hook's `VALID_TABS` allowlist derives from this registry, so `normalizeStoredTab` (localStorage) and the `gobby:show-activity-tab` handler accept the new id automatically.
- `ActivityPanel.tsx`: add `case "terminal":` to `tabContent()` rendering `<TerminalTab sessions={sessions} focusSessionId={terminalFocusSessionId} onFocusHandled={onTerminalFocusHandled} />` behind `React.lazy` + `Suspense`, keeping wterm JS and the wasm fetch out of the initial bundle. Add `terminalFocusSessionId?: string | null` and `onTerminalFocusHandled?: () => void` to `ActivityPanelProps` (wired in 3.1).

**Acceptance:**

- 2.5.1 - `terminal` is a registered ActivityTab with icon and label, accepted by stored-tab normalization. file: `web/src/components/activity/ActivityPanelTabs.tsx`.
- 2.5.2 - ActivityPanel lazily renders TerminalTab for the `terminal` case with the two focus props. file: `web/src/components/activity/ActivityPanel.tsx`.

## P3: Jump Affordances and Modal Cleanup

`kind: framing`

**Goal**: One-click jumps from chat and the sessions menu into the terminal tab focused on a specific session, and removal of the superseded capture-pane/send-keys modal modes.

### 3.1 Widen show-activity-tab event and thread terminal focus [category: code] (depends: 2.5)

`kind: deliverable`

Targets: `web/src/components/activity/useActivityPanel.ts`, `web/src/components/chat/ChatPage.tsx`

Widen the existing `gobby:show-activity-tab` CustomEvent detail to `{tab, sessionId?}` — the codebase's sanctioned cross-component escape hatch (`useActivityPanel.ts:186-197`). Do **not** reuse the `focusSessionId` prop thread: that value is ChatPage routing state with SessionsTab-selection semantics.

- `useActivityPanel.ts`: in the event handler, when the normalized tab is `"terminal"` and `detail.sessionId` is a string, store it as `terminalSessionRequest` before calling `showTab`. Return `terminalSessionRequest` and `clearTerminalSessionRequest` from the hook. The request must be stored, not just broadcast — the event can fire before TerminalTab is mounted.
- `ChatPage.tsx`: pass `terminalFocusSessionId={activity.terminalSessionRequest}` and `onTerminalFocusHandled={activity.clearTerminalSessionRequest}` to `<ActivityPanel>`.

**Acceptance:**

- 3.1.1 - Event handler stores `sessionId` for the terminal tab and exposes request/clear from the hook. file: `web/src/components/activity/useActivityPanel.ts`.
- 3.1.2 - `{tab: "terminal", sessionId}` dispatch stores and clears the request. test: `web/src/components/activity/__tests__/useActivityPanel.test.tsx::stores terminal session request`.
- 3.1.3 - ChatPage threads the two focus props into ActivityPanel. file: `web/src/components/chat/ChatPage.tsx`.

### 3.2 Terminal button in the chat status strip [category: code] (depends: 3.1)

`kind: deliverable`

Targets: `web/src/components/chat/AgentStatusBar.tsx`, `web/src/components/chat/ChatMainColumn.tsx`

- `AgentStatusBar.tsx`: new optional `onOpenTerminal?: () => void` prop; when present, render a "Terminal" button (prompt icon, `btn btn-accent btn-sm` idiom) in the existing actions cluster (~:96-140) beside Attach/Detach. Load `get_skill(name="impeccable")` on gobby-skills before styling the button.
- `ChatMainColumn.tsx` (~:169-188): wire `onOpenTerminal` only when `viewingMeta?.sessionType === "terminal" && chat.dbSessionId`, dispatching `window.dispatchEvent(new CustomEvent("gobby:show-activity-tab", { detail: { tab: "terminal", sessionId: chat.dbSessionId } }))`.

**Acceptance:**

- 3.2.1 - Status strip renders the Terminal button iff the handler prop is provided. test: `web/src/components/chat/__tests__/AgentStatusBar.test.tsx::terminal button`.
- 3.2.2 - ChatMainColumn wires the dispatch for tmux-backed viewed sessions only. file: `web/src/components/chat/ChatMainColumn.tsx`.

### 3.3 Reduce SessionInteractionModal to Send Context and add Open Terminal [category: code] (depends: 3.1)

`kind: deliverable`

Targets: `web/src/components/activity/SessionInteractionModal.tsx`, `web/src/components/activity/SessionsTabMenu.tsx`, `web/src/components/activity/SessionsTab.tsx`, `web/src/components/activity/__tests__/SessionInteractionModal.test.tsx`, `web/src/components/activity/__tests__/SessionsTab.test.tsx`

"Capture Pane" (one-shot `capture_output` into a `<pre>` + manual Refresh) is strictly dominated by the live view; "Send Keys" (blind send-keys form, no feedback) is strictly dominated by take-control/composer typing with live echo. "Send Context" stays: semantic message-passing that also works for non-tmux sessions.

- `SessionInteractionModal.tsx`: delete `InteractionMode` (:19), the keys/pane render branches, the `capture_output` fetch (:138, :265-276), and the `mode` prop — single-purpose Send Context modal (~−150 lines).
- `SessionsTabMenu.tsx`: delete "Send Keys" and "Capture Pane" items (:65-66); `openModal(mode, entry)` prop becomes `openContextModal(entry)`; add for `entry.hasTmux`: "Open Terminal" → dispatch `gobby:show-activity-tab` with `{tab: "terminal", sessionId: entry.id}`. `SessionsInteractionModalHost` (:119-145) drops `modalMode` and renders when `modalEntry` is set.
- `SessionsTab.tsx`: delete `modalMode` state (:139, :165); `openModal` (:491) → `openContextModal`; update host mount (:608).
- Update `web/src/components/activity/__tests__/SessionInteractionModal.test.tsx` (drop keys/pane cases) and `web/src/components/activity/__tests__/SessionsTab.test.tsx` (modal mock at :146, menu labels, new dispatch assertion).

**Acceptance:**

- 3.3.1 - Modal is context-only; keys/pane modes and `capture_output` call are gone. file: `web/src/components/activity/SessionInteractionModal.tsx`.
- 3.3.2 - Menu offers "Open Terminal" for tmux entries; the dispatched event flows through the stored `terminalSessionRequest` (3.1) into TerminalTab focus consumption — asserted end-to-end (dispatch → stored request → focused selection), not dispatch alone. test: `web/src/components/activity/__tests__/SessionsTab.test.tsx::open terminal focuses session`.

### 3.4 Remove stale terminal references [category: refactor]

`kind: deliverable`

Target: `web/eslint.config.js`

Delete the dangling per-file lint entry (:44) that still points at the deleted `TerminalsPage` component from the removed xterm page. (The dead `vendor-xterm` Vite rule is removed in 1.1; the stale Playwright spec is rewritten in 3.5.)

**Acceptance:**

- 3.4.1 - No references to the deleted `terminals/` component tree remain in lint config. file: `web/eslint.config.js`.

### 3.5 Rewrite terminal-colors Playwright spec for the terminal tab [category: test] (depends: 2.5)

`kind: deliverable`

Target: `web/tests/terminal-colors.spec.ts`

The spec currently navigates to the removed Terminals page and waits on `.xterm-screen` (:130-135). Rewrite against the new tab: keep the `page.routeWebSocket("**/ws")` mock, update the mocked `tmux_sessions_list` shape to include `socket`, `gobby_session_id`, `pane_dead`; open the activity panel, click the Terminal tab, select the mocked session, then assert rendered output via `getByText` inside `[data-testid="terminal-view"]` — the DOM renderer emits real text nodes, so no internal wterm class names are needed. Additionally assert computed styling so a missing `@wterm/dom/css` import fails the spec: a terminal layout metric (e.g. the row-grid `--term-row-height` taking effect) and an ANSI palette color on a styled cell. The ghostty wasm loads for real in the Playwright browser, doubling as an asset-path regression test.

Two additional cases in the same spec file, reusing the same mock harness:

- **Watch-and-control contract**: assert the outgoing `tmux_attach` request on selection; emit `terminal_output` frames and assert they render; open the composer, send text and a quick key, and assert the exact outgoing `terminal_input` payloads carry the current streaming id and the pinned bytes from 2.3 (text + `"\r"`; e.g. Esc → `"\x1b"`, Ctrl+C → `"\x03"`); switch to a second mocked session and assert `tmux_detach` is sent before the next `tmux_attach`. This pins the full watch-and-control path across every seam the unit tests mock individually.
- **Wasm-abort fallback**: abort the ghostty wasm request via route interception, open the tab, and assert the reduced-fidelity indicator appears AND streamed output still renders as DOM text — the "never a blank pane" constraint pinned in a real browser. Retry behavior stays in the 1.1 unit test.

**Acceptance:**

- 3.5.1 - Spec exercises the terminal tab end-to-end with a mocked WS, asserts ANSI-colored text renders as DOM text, and asserts computed terminal layout plus an ANSI palette color (stylesheet regression). test: `web/tests/terminal-colors.spec.ts`.
- 3.5.2 - The control-contract case pins attach request → output render → composer `terminal_input` payloads → detach-before-reattach ordering. test: `web/tests/terminal-colors.spec.ts`.
- 3.5.3 - The wasm-abort case proves the built-in-core fallback still renders streamed text with the fidelity indicator visible. test: `web/tests/terminal-colors.spec.ts`.

## E1: End-to-End Verification

`kind: verification`

Automated: `cd web && npm run type-check && npm run lint && npm test && npm run build` (the production build validates the wasm public asset and the `vendor-wterm` chunk), then `npx playwright test tests/terminal-colors.spec.ts`.

Manual, against the real daemon: spawn a tmux-backed agent session AND open an interactive session (they live on separate tmux sockets — confirm both appear in the picker and switching between sockets attaches correctly), open Activity → Terminal, and confirm: live streaming of a Claude Code TUI (colors, alt-screen, spinner); open the composer, answer a prompt with a quick key, send text, close the composer; panel resize reflows (debounced) and a fresh attach paints at panel size with no dropped-first-frame blank (ready-handshake repaint); switching sessions detaches then attaches; killing the tmux session shows the ended state; killing it while the page is reloading (vanished-during-reconnect) also lands in the ended state rather than silently re-attaching or auto-selecting; reloading mid-stream reconnects and repaints. Chat surface: viewing a terminal session shows the Terminal button in the status strip and jumps to the tab focused on that session; the sessions context menu "Open Terminal" does the same; "Capture Pane"/"Send Keys" are gone; "Send Context" still works. Mobile viewport (device emulation): stream renders, control mode shows the keys bar, quick keys reach the pane. Ghostty-failure path: block the wasm URL in devtools and confirm the built-in-core fallback with fidelity chip renders instead of a blank pane.

## V1 Plan Changelog

`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: 235418d8-0f7f-4c65-9e91-5b0b99519c6e (xhigh); an earlier independent pass 08ac5ab3-b289-4d13-b40f-7b9cf1094e70 also completed and its suggestions were merged
- enhancer_session: edb0f7e6-bd33-4a7c-9086-4969cba3a6a8; 91b7119e-04c1-4336-b21f-e74c4d692544
- converged: false (round cap max_enhancement_rounds=1 reached)
- suggestions_presented: 9 (deduped from 10 across the two passes; socket-qualified identity was found independently by both)
- accepted:
  - socket-qualified attach identity / better — `{name, socket}` sessionKey through hook, state machine, vanished detection (1.2, 2.4)
  - attach repaint handshake / better — view-ready + attach-result sequencing before clearing attaching, repaint via immediate resize (2.2, 2.4)
  - reconnect guard on unmount / better — `shouldReconnect` ref, no zombie socket after intentional close (1.2)
  - `sessionsLoaded` readiness flag / better — gate auto-select, empty state, and focus-request resolution (1.2, 2.4)
  - `ActivityRowStatusDot` reuse in picker / reuse — lifecycle status at selection time (2.3)
  - hook wire-payload tests / testability — exact `terminal_input`/`tmux_resize` payloads + no-send guards (1.2)
  - Playwright wasm-abort fallback case / testability — never-blank constraint pinned in a real browser (3.5)
  - Playwright watch-and-control contract / testability — attach → output → composer input → detach-before-reattach (3.5)
- declined:
  - keyboard release chord (Ctrl+Shift+Esc) / better — mooted by the user's input-model decision below; the terminal never captures keyboard input, so no keyboard trap exists to escape
- resolution_notes: While reviewing suggestions the user redirected the input model: the terminal is now always read-only display on every device, and all input flows through the `TerminalKeysBar` composer (the existing web-attach/send_keys pattern) — direct wterm keyboard passthrough was removed from the plan entirely. The user also required socket separation to be treated as a first-class constraint (agent sessions and interactive sessions run on separate tmux sockets), added to C1. All accepted suggestions were folded into sections 1.2, 2.2, 2.3, 2.4, and 3.5; plan re-validated.

**Round 1** `kind: verification`

- reviewer_run: e624687f-2f17-45ae-8777-0a5656fbeaa9 (xhigh)
- reviewer_session: c02a3413-7ca2-4a21-8fbe-99485bd8ae39
- verdict: needs_review
- findings:
  - ALT-R1-F1/blocking/missing-requirement — gobby-socket agent rows have `gobby_session_id: null` (default-socket-only pane map); join and focus must fall back to `agent_run_id`.
  - ALT-R1-F2/blocking/gobby-format — implementation-time unknowns and `<wasm-file>` placeholder; lockfile omitted; no production build in E1.
  - ALT-R1-F3/blocking/unhandled-edge — daemon `type:error` replies unhandled; attach effect not gated on `sessionsLoaded`/row presence on reconnect.
  - ALT-R1-F4/blocking/bad-sequencing — repaint handshake unimplementable from the specified interface (no synchronous size handoff; debounce-only dimensions).
  - ALT-R1-F5/blocking/bad-sequencing — 3.3 could expand parallel to 3.1; menu jump would dispatch without stored focus.
- resolution_notes: F1 — verified against `_handle_tmux_list_sessions` (tmux.py:190-300) and fixed with a two-stage join (primary `gobby_session_id`, fallback `agent_run_id`) plus `findByGobbySessionId` focus resolution over the joined list; C1 documents the daemon behavior; tests added (2.1.3, 2.4.2). F2 — resolved by reading upstream source: `@wterm/ghostty` exports only the root subpath, so the design is a committed postinstall copy script (`web/scripts/copy-ghostty-wasm.cjs` → `public/wasm/ghostty-vt.wasm`) with `GhosttyCore.load({wasmPath})`; wterm's `InputHandler` routes all input through `onData`, so a no-op `onData` is the definitive read-only mechanism (no readOnly option exists or is needed); lockfile added to 1.1 targets; `npm run build` added to E1 and 1.1.5. F3 — hook patch 6 correlates `error` frames by `request_id`, clears pending state, exposes `attachError`; attach effect gated on `connected && sessionsLoaded && row present`; tests 1.2.5, 2.4.5. F4 — `onReady(rows, cols)` synchronous size handoff + `getSize()`; per-instantiation repaint semantics (initial/fallback/Retry) specified in 2.2 and 2.4; tests updated (2.2.2, 2.4.3). F5 — 3.3 now depends on 3.1 and its acceptance asserts dispatch → stored request → focused selection end-to-end (3.3.2).

**Round 2** `kind: verification`

- reviewer_run: 3f474006-cb60-43ca-bb32-470175f4a4fc (xhigh)
- reviewer_session: bdd94094-8000-4a72-ba9c-fa9f72342f31
- verdict: needs_review
- findings:
  - ALT-R2-F1/blocking — R1-F1 incompletely resolved: both daemon identity fields are cross-socket unsafe (bare server-local pane IDs applied to both sockets; name-only agent_run_id match); primary-then-fallback join can focus the wrong session under collisions.
  - ALT-R2-F2/blocking — R1-F2 read-only conclusion incomplete: wterm always creates/focuses/click-refocuses a hidden textarea and preventDefaults keys; no-op `onData` leaves keyboard capture and a focus trap, and discards VT protocol replies; stale readOnly conditional retained in 2.2.
  - ALT-R2-F3/blocking — `attachError` unclearable from a halted machine (circular); no pending-request guard against duplicate attach/detach sends.
  - ALT-R2-F4/blocking — vanished-during-reconnect unreachable: hook clears `attachedTarget` on close, so the post-reconnect comparison source was already null.
  - ALT-R2-F5/blocking — upstream callback shapes wrong in plan: `onResize(cols, rows)` (cols first) and `onReady(wt)` (dims off the instance); transposition risk into `tmux_resize`.
  - ALT-R2-F6/blocking — required `@wterm/dom/css` stylesheet never imported; acceptance couldn't detect the omission.
  - ALT-R2-F7/blocking — quick-key wire bytes undefined; no oracle for 3.5's exact payload assertions; 1/2/3 Enter-inclusion ambiguous.
  - ALT-R2-F8/blocking — all-dead session list left the tab selection-less with no defined state despite dead-pane attach being allowed.
  - ALT-R2-F9/blocking — stateful `GhosttyCore` singleton leaks a wasm terminal allocation per keyed remount (`init` overwrites termPtr without free; `destroy` never deinits) and couples renderer lifetimes.
- resolution_notes: All nine findings verified against wterm 0.3.0 source (`gh api` on input.ts, wterm.ts, Terminal.tsx, ghostty-core.ts, dom package.json) and `_handle_tmux_list_sessions` before applying. F1 — C1 and 2.1 now specify the socket-specific trust matrix (default→`gobby_session_id` only, gobby→`agent_run_id` only, other field ignored per socket) with cross-socket collision tests incl. focus resolution (2.1.3). F2 — read-only is now direct textarea neutralization (disabled + tabIndex −1 + blur, per renderer instantiation in the ready callback) plus `role="log"` ARIA override via the wrapper's late prop spread; `onData` is always supplied and routed as a protocol-response relay to `terminal_input` (viewer answers tmux device queries; multi-viewer duplicate replies recorded as accepted C1 limitation); readOnly conditional removed; mount/click/Tab/keyboard/selection/protocol-response pinned (2.2.2, 2.4.4). F3 — hook exposes `clearAttachError()` + `requestPending`; machine gates on both; Retry/selection-change clearing specified; duplicate-send and retry-after-error tests (1.2.5, 1.2.6, 2.4.5). F4 — `lastAttachedKeyRef` in TerminalTab (survives socket close) compared against each first post-reconnect list; Dismiss semantics defined; duplicate-name-across-sockets reconnect test (2.4.5). F5 — C1 fact c states the real shapes; 2.2 specifies both adapters (`wt.rows`/`wt.cols` from onReady; cols-first transposition) with non-square fixtures asserting `tmux_resize` order (2.2.3, 2.4.3). F6 — `import "@wterm/dom/css"` mandated in the lazy module with theme overrides after it; acceptance + Playwright computed-style/palette assertions (2.2.1, 3.5.1). F7 — exact bytes pinned in 2.3 (Esc `\x1b`, Tab `\t`, Enter `\r`, `\x1b[A`/`\x1b[B`, Ctrl+C `\x03`, bare-digit 1/2/3) and mirrored in 2.3.2 and 3.5. F8 — fallback selection defined as first non-dead else first row; all-dead test with composer disabled (2.4.2). F9 — singleton replaced with a fresh `GhosttyCore.load` per renderer instantiation relying on HTTP wasm caching; retry state local to TerminalView; distinct-instance tests (1.1.3, 1.1.4, 2.2.1).

**Round 3** `kind: verification`

- reviewer_run: d5395ecf-2dfb-4d44-a293-da1cacfe5002 (xhigh)
- reviewer_session: 75555024-e02d-4784-94e6-0367da478165 (#9136)
- verdict: needs_review
- findings:
  - ALT-R3-F1/blocking/unhandled-edge — default-row `gobby_session_id` still not collision-safe: the pane map includes spawned-agent sessions (gobby-socket `TMUX_PANE` backfill) keyed by bare `%N`, so an agent session can overwrite the value a colliding default row picks up; `agent_managed` equally unsafe on default rows yet exposed as a badge.
  - ALT-R3-F2/blocking/bad-sequencing — `@wterm/react` 0.3.0 lifecycle-incompatible with React 18.3.1: init/destroy live in a React 19 callback-ref cleanup return that React 18 ignores (null-detach branch early-returns without destroying), leaking WTerm instances/observers/listeners on every keyed remount, fallback swap, Retry, and unmount — defeating the fresh-core fix.
  - ALT-R3-F3/blocking/unhandled-edge — `requestPending` cleared only by a correlated result/error frame: a socket close mid-request wedges the machine permanently; a stale selection that never successfully attached (empty `lastAttachedKeyRef`) reaches neither `sessionEnded` nor fallback selection.
  - ALT-R3-F4/blocking/unhandled-edge — boolean `shouldReconnect` ref unsafe under the app's StrictMode: dev effect replay either kills future reconnects or lets a delayed `onclose` from the disposed socket schedule a zombie.
  - ALT-R3-F5/blocking/missing-requirement — wrapper's hardcoded `aria-multiline="true"` survives the prop spread; invalid alongside `role="log"`, undetected by 2.2.2.
- resolution_notes: All five findings source-verified before acceptance: F1 against `_handle_tmux_list_sessions` (map built from every active/paused session with `terminal_context.tmux_pane`, no agent exclusion, bare `%N` keys) and the frontend `GobbySession` type (carries `agent_run_id`, enabling the provenance check client-side); F2/F5 against upstream `Terminal.tsx` (verbatim "React 19 callback ref with cleanup" comment, `if (!el) return` null branch, hardcoded `aria-multiline="true"`) and `web/package.json` (`react ^18.3.1`); F4 against `web/src/main.tsx` (`<StrictMode>`) and the existing `connectionGeneration` pattern in `web/src/hooks/useWebSocketEvent.ts`; F3 against the plan's own 1.2/2.4 text. Resolutions: F1 — C1 and 2.1 now require default-row joins to match only non-agent-backed sessions (`GobbySession.agent_run_id == null`) and derive `agentManaged` solely from trusted gobby-row joins, with the colliding-agent-session default-row case pinned (2.1.1, 2.1.3). F2 — `@wterm/react` dropped from the dependency set (1.1, O1); 2.2 rewritten to drive `WTerm` from `@wterm/dom` directly with an explicit effect lifecycle: per-instance `disposed` flag, exactly-once `destroy()` (synchronous when init settled, else chained onto the init promise), stale-async suppression; pinned across StrictMode double-mount, fallback, Retry, keyed replacement, and unmount (2.2.4). F3 — 1.2 patch 7 now invalidates the request token and synchronously clears `requestPending` + in-flight error state on close, ignoring stale-token frames (1.2.7); 2.4 vanished detection generalized to both `lastAttachedKeyRef` and `selectedKey` so never-attached stale selections reach `sessionEnded`, with close-during-attach pinned for present and absent targets (2.4.1, 2.4.5). F4 — 1.2 patch 5 replaced with connection-generation tokens following `useWebSocketEvent.ts`, StrictMode replay/zombie-close/final-unmount cases pinned (1.2.2). F5 — moot under the direct adapter, made explicit: 2.2 renders its own container with `role="log"` and no `aria-multiline`; 2.2.2 asserts the final attribute set excludes textbox-only state.

**Round 4** `kind: verification`

- reviewer_run: 5028d7c1-23a5-4a2f-98eb-764f827ec6b8 (xhigh)
- reviewer_session: d69ad3cb-6b5c-4df3-89cc-13be816606bc (#9161)
- verdict: needs_review
- findings:
  - ALT-R4-F1/blocking/bad-sequencing — the round-3 direct-WTerm cleanup deferred `wt.destroy()` behind a pending init promise (`initPromise.finally`). React StrictMode replays setup→cleanup→setup synchronously while promise handlers run later, so the obsolete instance can finish init and then destroy against the same container **after** the replacement mounted; upstream `WTerm.destroy()` executes `element.innerHTML = ""`, erasing the live replacement during StrictMode replay, fallback swap, or Retry. `WTerm.init()` already re-checks `_destroyed` after its async core load, making immediate cleanup safe. (ALT-R3-F1/F3/F4/F5 resolutions verified; R3-F2 partially — dropping `@wterm/react` confirmed correct, but its replacement lifecycle introduced this finding.)
- resolution_notes: Verified against upstream `packages/@wterm/dom/src/wterm.ts` (0.3.0) before acceptance: `destroy()` wipes `element.innerHTML` and never nulls `bridge`; `init()` re-checks `_destroyed` immediately after the awaited core load; verification also surfaced an aggravator beyond the reported finding — `init()`'s rejection path calls `this.destroy()` internally with **no** `_destroyed` guard, a second destroy outside wrapper control that can fire after cleanup and equally wipe a successor on a shared element. Resolutions: C1 fact (h) added recording all four verified behaviors. 2.2 Instance lifecycle rewritten — cleanup is synchronous, immediate, exactly-once even while `init()` is pending (relying on the `_destroyed` re-check; deferred destroy removed), the resize debounce timer is cancelled in cleanup (2.2 Resize: a trailing fire would report dead-instance dimensions and trigger a stale `tmux_resize`), `disposed` checks retained on all continuations, and each instance mounts in its own wrapper-owned child node under the ARIA container so the uninterceptable init-rejection destroy wipes only a detached node. 2.2.4 extended per the required resolution: late init settlement after StrictMode replay/fallback replacement asserts the second instance's DOM, callbacks, and resize state intact; late init **rejection** asserts the replacement survives the internal error-path destroy; cleanup-cancelled debounce asserts no post-destroy `onSizeChange`.

**Round 5** `kind: verification`

- reviewer_run: 7c1399b2-bc8c-4332-aea8-1f64c1293624
- reviewer_session: 98c632dc-f526-41e0-b50a-fb65628869e9 (#9168)
- verdict: approved
- findings: none
- resolution_notes: All five elements of the ALT-R4-F1 resolution verified against the artifact and upstream `packages/@wterm/dom/src/wterm.ts` (0.3.0) — C1 fact (h) matches upstream destroy/init lifecycle semantics exactly; 2.2 mandates synchronous, immediate, exactly-once cleanup destruction while `init()` is pending and explicitly forbids deferred destruction; cleanup cancels the trailing resize debounce; each `WTerm` receives a wrapper-owned child mount node under the ARIA container, containing any late internal init-rejection destroy to the detached obsolete node; 2.2.4 pins late settlement, late rejection after replacement, survivor DOM/callback/resize integrity, stale `onReady` suppression, and cancelled-debounce behavior. CSS/DOM recheck of the per-instance mount node: upstream adds `.wterm` to the element it receives, sets `--term-row-height` there, observes and scrolls that element, and appends `.term-grid` beneath it — the per-instance child remains the complete stylesheet/resize/scroll host, and `height:100%` plus Gobby's global border-box sizing preserves height accounting. Whole-artifact second pass found no new blocking branch, sequencing, traceability, testability, or proportionality findings. Reviewer wrote the 12-entry `## M1 Task Manifest` (10 code entries `implementation_domain: frontend`; refactor/test entries route to frontend-developer; no `covers:unknown` labels) and confirmed `uv run gobby plans validate --mode expansion` passes; the coordinator independently re-ran expansion-mode validation and re-verified the manifest. Convergence reached — no changes required.

## M1 Task Manifest

`kind: manifest`

```yaml
- title: Add wterm dependencies and ghostty core loader
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'Passes acceptance 1.1.1-1.1.5: dependencies, wasm copy, fresh-core
    loading, chunking, focused tests, and the production build all succeed.'
  labels:
  - covers:activity-panel-live-terminal:1.1:1.1.1
  - covers:activity-panel-live-terminal:1.1:1.1.2
  - covers:activity-panel-live-terminal:1.1:1.1.3
  - covers:activity-panel-live-terminal:1.1:1.1.4
  - covers:activity-panel-live-terminal:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: frontend
- title: Revive useTmuxSessions with connection-state patches
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'Passes acceptance 1.2.1-1.2.7: socket-qualified state, reconnect
    generations, request recovery, wire payloads, and focused hook tests all succeed.'
  labels:
  - covers:activity-panel-live-terminal:1.2:1.2.1
  - covers:activity-panel-live-terminal:1.2:1.2.2
  - covers:activity-panel-live-terminal:1.2:1.2.3
  - covers:activity-panel-live-terminal:1.2:1.2.4
  - covers:activity-panel-live-terminal:1.2:1.2.5
  - covers:activity-panel-live-terminal:1.2:1.2.6
  - covers:activity-panel-live-terminal:1.2:1.2.7
  tdd: true
  source_section: '1.2'
  implementation_domain: frontend
- title: Terminal session join helpers
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: 'Passes acceptance 2.1.1-2.1.3: provenance-safe joins, presentation
    data, focus lookup, and all collision fixtures succeed.'
  labels:
  - covers:activity-panel-live-terminal:2.1:2.1.1
  - covers:activity-panel-live-terminal:2.1:2.1.2
  - covers:activity-panel-live-terminal:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: frontend
- title: TerminalView wterm wrapper
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: 'Passes acceptance 2.2.1-2.2.4: rendering, read-only ARIA,
    per-instance lifecycle containment, resize behavior, fallbacks, and focused tests
    all succeed.'
  labels:
  - covers:activity-panel-live-terminal:2.2:2.2.1
  - covers:activity-panel-live-terminal:2.2:2.2.2
  - covers:activity-panel-live-terminal:2.2:2.2.3
  - covers:activity-panel-live-terminal:2.2:2.2.4
  tdd: true
  source_section: '2.2'
  implementation_domain: frontend
- title: Session picker and touch composer bar
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: 'Passes acceptance 2.3.1-2.3.3: controlled selection, trusted
    status presentation, composer input, exact key bytes, and component tests all
    succeed.'
  labels:
  - covers:activity-panel-live-terminal:2.3:2.3.1
  - covers:activity-panel-live-terminal:2.3:2.3.2
  - covers:activity-panel-live-terminal:2.3:2.3.3
  tdd: true
  source_section: '2.3'
  implementation_domain: frontend
- title: TerminalTab root with attach lifecycle
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '2.2'
  - '2.3'
  validation_criteria: 'Passes acceptance 2.4.1-2.4.5: attach state, filtering, repaint,
    reconnect and vanished-session handling, composer control, UI states, and focused
    tests all succeed.'
  labels:
  - covers:activity-panel-live-terminal:2.4:2.4.1
  - covers:activity-panel-live-terminal:2.4:2.4.2
  - covers:activity-panel-live-terminal:2.4:2.4.3
  - covers:activity-panel-live-terminal:2.4:2.4.4
  - covers:activity-panel-live-terminal:2.4:2.4.5
  tdd: true
  source_section: '2.4'
  implementation_domain: frontend
- title: Register the terminal tab in the ActivityPanel
  category: code
  task_type: feature
  depends_on:
  - '2.4'
  validation_criteria: 'Passes acceptance 2.5.1-2.5.2: terminal tab registration,
    stored-tab normalization, lazy rendering, and focus-prop wiring all succeed.'
  labels:
  - covers:activity-panel-live-terminal:2.5:2.5.1
  - covers:activity-panel-live-terminal:2.5:2.5.2
  tdd: true
  source_section: '2.5'
  implementation_domain: frontend
- title: Widen show-activity-tab event and thread terminal focus
  category: code
  task_type: feature
  depends_on:
  - '2.5'
  validation_criteria: 'Passes acceptance 3.1.1-3.1.3: terminal focus requests are
    stored, cleared, tested, and threaded from ChatPage into ActivityPanel.'
  labels:
  - covers:activity-panel-live-terminal:3.1:3.1.1
  - covers:activity-panel-live-terminal:3.1:3.1.2
  - covers:activity-panel-live-terminal:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: frontend
- title: Terminal button in the chat status strip
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: 'Passes acceptance 3.2.1-3.2.2: the status action renders conditionally
    and dispatches terminal focus only for tmux-backed viewed sessions.'
  labels:
  - covers:activity-panel-live-terminal:3.2:3.2.1
  - covers:activity-panel-live-terminal:3.2:3.2.2
  tdd: true
  source_section: '3.2'
  implementation_domain: frontend
- title: Reduce SessionInteractionModal to Send Context and add Open Terminal
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: 'Passes acceptance 3.3.1-3.3.2: the modal is context-only and
    the sessions menu provides a tested end-to-end Open Terminal focus flow.'
  labels:
  - covers:activity-panel-live-terminal:3.3:3.3.1
  - covers:activity-panel-live-terminal:3.3:3.3.2
  tdd: true
  source_section: '3.3'
  implementation_domain: frontend
- title: Remove stale terminal references
  category: refactor
  task_type: feature
  depends_on: []
  validation_criteria: 'Passes acceptance 3.4.1: the lint configuration contains no
    reference to the deleted terminals component tree.'
  labels:
  - covers:activity-panel-live-terminal:3.4:3.4.1
  tdd: false
  source_section: '3.4'
  assigned_agent: frontend-developer
- title: Rewrite terminal-colors Playwright spec for the terminal tab
  category: test
  task_type: feature
  depends_on:
  - '2.5'
  validation_criteria: 'Passes acceptance 3.5.1-3.5.3: Playwright verifies DOM styling,
    watch-and-control ordering and bytes, and wasm-abort fallback rendering.'
  labels:
  - covers:activity-panel-live-terminal:3.5:3.5.1
  - covers:activity-panel-live-terminal:3.5:3.5.2
  - covers:activity-panel-live-terminal:3.5:3.5.3
  tdd: false
  source_section: '3.5'
  assigned_agent: frontend-developer
```
