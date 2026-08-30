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
`list_tools("chrome-devtools")`). See the `mcp-servers` skill.

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
