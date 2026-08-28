# Live actions

Before running any `node <scripts_dir>` command, use
`materialize_skill_scripts(name="impeccable")` and set
`environment.PUPPETEER_CACHE_DIR` from its result.

### Aborting an in-flight session

If wrap or generation fails after the browser flipped to GENERATING, tell the **browser** so its bar resets: `node <scripts_dir>/live-poll.mjs --reply EVENT_ID error "Short reason"`. Never use `live-accept --discard` for this (pure file mutator, browser never sees it, bar sticks on dots); `--discard` is only source-side cleanup for a discard the browser itself initiated.

## Handle fallback

When wrap returns `fallback: "agent-driven"`, you pick the source file yourself; the goal is unchanged: three preview variants now, and the accepted one persisted where the next build cannot wipe it.

1. **Find where the element really lives** from the error payload: `element_not_in_source` + `generatedMatch` means the served HTML is generated, so find the generator's template or partial; `element_not_found` means runtime-injected, so find the rendering component or data source; `file_is_generated` resolves the same way. A purely visual change may belong in a shared stylesheet rather than a template.
2. **Preview in the served file**: manually write the same wrapper scaffold `live-wrap.mjs` produces (`<!-- impeccable-variants-start ID --><div data-impeccable-variants="ID" data-impeccable-variant-count="3" style="display: contents">…</div><!-- end -->`) into the file the browser actually loaded, insert your variant divs, `--reply EVENT_ID done --file <served file>`. This edit is temporary; a regen wiping it is fine.
3. **On accept, write to true source** (accept refuses generated files, so `_acceptResult.handled` is usually `false` here): structural change → template/component source; visual-only → the right stylesheet; content rendered from data → the data source or render logic. Then remove the temporary wrapper from the served file.
4. **On discard**, just remove the temporary wrapper.

## Handle `accept`

Event: `{id, variantId, _acceptResult, _completionAck}`. The poll script already ran `live-accept.mjs` deterministically and acknowledged delivery; the browser DOM is already updated.

- The accept event includes `pageUrl`; the poll script must forward it to `live-accept.mjs --page-url PAGE_URL` so accept-time cleanup only scrubs staged copy edits for the current page.
- `_completionAck.ok !== true`: do not poll yet. Run `live-status.mjs` / `live-resume.mjs`, finish cleanup manually if needed, then `live-complete.mjs --id EVENT_ID`.
- `handled: true, carbonize: false`: nothing to do; poll again.
- `handled: true, carbonize: true`: required cleanup below; `_acceptResult.todo`, `_completionAck.requiresComplete`, and the stderr banner all point at it.
- `handled: false, mode: "fallback"`: the session lived in a generated file; you already wrote true source in fallback Step 3; clean the temporary wrapper and poll.
- `handled: false, mode: "error"`: **do not hand-edit the file.** `source_locked`: rerun the same `live-accept.mjs` command (idempotent) until the publisher releases. `accept_receipt_conflict`: the session already resolved as `priorOperation`; run `live-status.mjs` and tell the user. Anything else: report briefly, run `live-status.mjs` first.
- `handled: false` without `mode`: manual cleanup: read file, find markers, edit.

### Required after accept (carbonize)

`carbonize: true` means the accepted variant is stitched into source with helper markers and inline CSS (so the browser renders with no gap). That stitch-in is temporary; rewrite it into permanent form before anything else, or dead `@scope` rules, wrapper divs, and marker comments accumulate across sessions. Five steps, synchronously, before the next poll:

1. **Locate the carbonize block** in `_acceptResult.file`: bracketed by `<!-- impeccable-carbonize-start/end SESSION_ID -->` with a `<style data-impeccable-css>` element; read the `<!-- impeccable-param-values -->` comment first when present, it drives steps 3 and 4.
2. **Move the CSS rules** into the project's real stylesheet (whichever already owns styling for the surrounding element).
3. **Bake param values while rewriting selectors**: retarget `@scope ([data-impeccable-variant="N"])` to real semantic classes; keep only the `:scope[data-p-<id>="VALUE"]` branch matching the chosen value; substitute `var(--p-<id>)` literals or update the var's default.
4. **Unwrap the accepted content**: delete the inner variant div (and on JSX the outer `data-impeccable-carbonize` div); drop `data-impeccable-params` and all `data-p-*` attributes.
5. **Delete** the inline `<style>` block, the param-values comment, both carbonize markers, and any `@scope` rules for non-accepted variants.

Then run `live-complete.mjs --id SESSION_ID` and verify `phase: "completed"` before polling again. The command is a gate, not a formality: it refuses with `error: "source_dirty"` plus findings while any live-mode leftover remains; fix and rerun (`--force` only for false positives).

## Handle `discard`

Event: `{id, _acceptResult, _completionAck}`. The poll script already restored the original and acknowledged `discarded`. Nothing to do unless `_completionAck.ok !== true`; then `live-complete.mjs --id EVENT_ID --discarded` and poll again.

## Handle `steer`

Event: `{id, message, pageUrl}`: page-level direction from the global bar's Steer control (typed or spoken), no element context, no variant cycling. Read `message`, inspect the page or files as needed, make edits or answer in prose. Reply `node <scripts_dir>/live-poll.mjs --reply EVENT_ID steer_done ["Optional short toast"]`, or on failure `--reply EVENT_ID error "Short reason"`, then poll immediately. No separate pickup reply; the Steer bar unlocks on `steer_done` or `error`.

## Handle `prefetch`

Event: `{pageUrl}`: fired once per route on first selection; the user is likely about to Go on a page you have not read. Resolve the route to its file (root `/` is usually the boot's `pageFile`; multi-page sites often map `/foo` to `public/foo/index.html`; SPAs map everything to one entry), read it, poll again. No `--reply`. If you cannot resolve it confidently, skip and poll.

## Handle `manual_edit_apply`

Event: `{id, pageUrl, batch: {entries}, evidencePath?, chunk?, repair?, deadlineMs}`.

The user already clicked Apply. Do not ask what to do, discard, or redirect to Go. The parent live thread keeps the foreground poll loop and sends the final `/poll --reply --data`.

When native subagents are available, delegate source edits to `impeccable_manual_edit_applier` / `impeccable-manual-edit-applier`. Pass cwd, scripts path, event id, page URL, chunk/deadline, `batch`, `evidencePath`, and the canonical JSON result schema. The subagent must not poll or reply. If unavailable, apply inline with the same contract.

If `repair` is present, the previous Apply changed source but final validation failed. Fix the current source and return the same canonical JSON result; do not roll files back yourself. The browser will ask the user before any rollback.

After source edits finish, reply exactly once with `node <scripts_dir>/live-poll.mjs --reply EVENT_ID done --data '{"status":"done","appliedEntryIds":["8hexid"],"failed":[],"files":["src/page.html"],"notes":[]}'`. Use `status:"partial"` or `status:"error"` with `failed[]` when not every entry applied. Then poll again. Never reply without the event id; `--reply done --file ...` is invalid for manual Apply.
