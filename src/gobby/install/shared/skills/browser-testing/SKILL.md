---
name: browser-testing
description: Drive a browser to verify UI changes, run E2E flows, capture screenshots, or debug web performance. Use when work needs browser automation, web testing, page interaction, or frontend verification in a real browser.
version: "1.0.0"
category: engineering
triggers: browser, playwright, chrome devtools, e2e, screenshot, web testing, ui verification, browser automation, page snapshot
metadata:
  gobby:
    audience: all
    depth: 0
---

# Browser Testing

How to drive a browser well with the tools Gobby already ships. `playwright`
and `chrome-devtools` are **templates**. Instantiate them before use
(instance names default to the template name). Then reach the instance with
progressive discovery (`list_tools("playwright")`,
`list_tools("chrome-devtools")`). See `gobby:references/mcp-servers/templates.md`.

## Local sign-in without a password

Use the bundled, locked-down browser instances for local Gobby proofs. They
launch a fresh isolated profile and permit HTTP(S) requests only to literal
`127.0.0.1`, `[::1]`, or `localhost`, including temporary daemon and Vite ports.
External navigation, redirects and subresources fail through a dead proxy;
Playwright's origin allow-list is an additional check, not the security boundary.
Do not attach to an existing Chrome instance or reuse a user's profile.

1. Start the isolated test daemon and Vite server with temporary state/ports and
   the test hub database. Never run acceptance mutations against the live daemon.
2. Point the CLI's normal daemon configuration at that loopback daemon. Run
   `gobby ui sign-in-link`. The CLI uses its existing operator credential or the
   managed run's private credential and identity headers. Never request, type,
   store or transmit an operator password.
3. Open the returned URL using `browser_navigate` (Playwright) or `navigate_page`
   (Chrome DevTools). The server consumes the link and redirects to `/`, setting
   an HttpOnly session cookie. Managed links create a signed browser-testing
   session bound to the minting run and project; operator links use the normal
   operator session. Verify an authenticated page or
   `/api/auth/status` before testing.
4. The link expires after 120 seconds and works once, on its exact loopback
   origin. Mint another if it expires. The browser session expires after one
   hour; `/api/auth/logout` revokes it immediately. Close the browser after the
   proof. A new MCP process starts with no prior cookies or local storage.

Managed browser sessions lose access when their run ends and cannot mint API
keys or more sign-in links. HTTP access is read-only (GET/HEAD/OPTIONS), except
POST `/api/auth/logout`; all other mutations are refused before dispatch.
WebSocket access permits only ping/heartbeat, subscriptions, terminal list/attach/
detach/focus/viewport/scroll, and workspace attach/snapshot. Commands, input,
chat, tool calls, and other messages are refused. Project and session selectors
must match the signed claims; individual object-ID routes retain their
existing authorization checks. The shared MCP manager also refuses host-code
execution, uploads, memory snapshots, caller-chosen file output paths, and
non-HTTP(S) loopback navigation, including `file://`. Omit filename/filePath/path
arguments for screenshots, snapshots, PDFs, traces, and network captures.

Treat the URL as a short-lived credential: pass it only as a browser tool input,
never a Chrome process argument, repository file, screenshot, or saved trace.
The exchange renders no page or JavaScript containing the token. Audit records
identify the verified minting key or managed session/run without token bytes.

After landing template changes, activation needs a bundled template sync,
`refresh_template_instances`, and reconnecting both MCP instances with
`gobby mcp-proxy refresh --server <instance>`. A template DB update alone leaves
an already-connected process unchanged. Coordinate activation with the
Orchestrator; new runtime hooks require the landed Python code as well.

## Tool Routing

Pick one path per job; never drive the same page with both at once.

| Job | Tool |
| --- | --- |
| Interactive UI verification, E2E flows, form filling, click-through testing | `playwright` MCP |
| Performance traces, network waterfall, console errors, CPU/memory profiling | `chrome-devtools` MCP |
| Long repetitive flows (20+ steps), data-extraction loops, screenshot batteries | One-off Playwright script via Bash |

## The Snapshot → Ref → Act Loop

This is the single biggest quality lever for browser work:

1. **Snapshot before acting.** Take an accessibility-tree snapshot
   (`browser_snapshot` on the playwright MCP) — not a screenshot — to see
   the page's interactive elements with stable element refs.
2. **Act on refs**, not on guessed selectors: click/fill/select using the
   refs the snapshot returned.
3. **Re-snapshot after navigation** or any action that changes the DOM.
   Stale refs are the top cause of flaky agent browser runs.

Screenshots are for *visual* checks (layout, color, rendering) only; the
a11y snapshot is cheaper and more reliable for interaction.

## Reconnaissance, Then Action

- Start the app under test yourself and wait for it to be ready before
  navigating (poll the port or watch for the listen log line; after
  navigation wait for network-idle rather than sleeping).
- On first contact with a page: navigate → wait → snapshot → identify the
  elements involved → only then act. Do not chain blind actions.
- Verify outcomes by observing the page (snapshot, URL, console), not by
  assuming the action worked.

## Script-Mode Escape Hatch

MCP round-trips cost a call per click. For long flows, write a one-off
Playwright script and run it via Bash instead:

- Use the project's installed `playwright`/`playwright-core` from
  `node_modules` when present (import by absolute path if needed) with
  `headless: true`.
- Print findings to stdout as JSON/text; exit non-zero on assertion failure.
- Good fits: 20+-step regression walks, viewport/color-scheme matrices,
  scraping structured data, repeated auth flows.

## Known Pitfalls

- **chrome-devtools MCP window ops hang on occluded windows.** If the driven
  Chrome window is minimized or fully covered, `resize_page`, `emulate`, and
  `take_screenshot` can block forever and wedge the whole MCP queue.
  Recovery: kill the chrome-devtools MCP process (the daemon respawns it on
  the next call). Prefer headless Playwright scripts for screenshots,
  viewport, and color-scheme checks; keep chrome-devtools for traces,
  network, and console work.
- **requestAnimationFrame is suspended in occluded windows** — rAF-gated
  focus/animation code silently does nothing there, so DOM-focus assertions
  that pass in jsdom can fail against a live occluded window. Test such
  paths headless or with the window visible.
- Never leave orphaned browser or dev-server processes running; stop what
  you started.
