# Live setup and recovery

Before running any `node <scripts_dir>` command, use
`materialize_skill_scripts(name="impeccable")` and set
`environment.PUPPETEER_CACHE_DIR` from its result.

## Start

```bash
node <scripts_dir>/live.mjs
```

Output JSON: `{ ok, serverPort, serverToken, pageFiles, roots, hasProduct, product, productPath, hasDesign, design, designPath, hasSurfaceBrief, surfaceBrief }`. `roots` is the resolved root manifest; `projectRoot` mirrors `roots.appRoot`. The surface brief rides along; do not shell out to `surface-brief.mjs` separately. The `product` and `design` fields are legacy upstream signals. **`.impeccable.md` governs durable product, voice, and visual decisions; the surface brief governs this surface's strategy within that contract.** When `.impeccable.md` is missing, extract identity from CSS variables, computed styles, and sibling components (Step 4 Phase A). Identity preservation is the default; departure requires the user's explicit redesign intent.

`serverPort`/`serverToken` belong to the small helper HTTP server (`/live.js`, SSE, `/poll`), not your dev server; the page URL is whatever origin serves a `pageFiles` entry.

If output is `{ ok: false, error: "config_missing" | "config_invalid", path }`, this project needs one-time configuration: call `get_skill_file(name="impeccable", path="references/live-setup.md")` on `gobby-skills` and follow it. If the output carries a non-null `configDrift`, tell the user once which HTML files are uncovered and suggest adding them or switching `files` to a glob; never auto-edit the config.

## Recovery commands

The append-only journal under `.impeccable/live/sessions/` is canonical durable state (not project source). When the chat was interrupted, polling was missed, the helper restarted, or the browser reloaded:

```bash
node <scripts_dir>/live-status.mjs      # helper state, active sessions, queued events; works with the helper down
node <scripts_dir>/live-resume.mjs --id SESSION_ID   # active snapshot, pending event, next safe action
node <scripts_dir>/live-complete.mjs --id SESSION_ID # canonical manual final acknowledgement after verified cleanup
```

Server restart rule: start `live-server.mjs` again, then poll; startup requeues unacknowledged events, so never ask the user to click Go again unless `live-resume.mjs` says no active session exists.

## Exit

The user stops live mode by saying so in chat, closing the tab (SSE drops; poll returns `exit` after 8s), or the browser's exit button. On `exit`, kill any still-running background poll, then clean up.

## Cleanup

```bash
node <scripts_dir>/live-server.mjs stop
```

Stops the helper and runs `live-inject.mjs --remove` to strip the injected script (use `stop --keep-inject` to keep it for a quick restart; `.impeccable/live/config.json` persists as project config). Then search for and remove any leftover `impeccable-variants-start` wrappers and `impeccable-carbonize-start` blocks.

This stops only the materialized live helper. It does not stop the app dev server
or alter Gobby daemon state.

## First-time setup

Only when `live.mjs` reports `config_missing` / `config_invalid`, or `configDrift` needs explaining, or the config lacks `cspChecked`: follow `references/live-setup.md`, loaded through `get_skill_file` as described above. It owns the config schema, the per-framework `files` table, injection adapters, drift healing, and the CSP detection and consent flow.
