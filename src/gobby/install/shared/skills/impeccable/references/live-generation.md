# Live generation

Before any `node <scripts_dir>` command, call
`materialize_skill_scripts(name="impeccable")` and use its
`environment.PUPPETEER_CACHE_DIR`. For setup drift or recovery,
call `get_skill_file(name="impeccable", path="references/live-setup-recovery.md")`.

## Handle `generate`

**Replace mode** (default): `{id, action, freeformPrompt?, count, pageUrl, element, screenshotPath?, comments?, strokes?}`.

**Insert mode** (`event.mode === "insert"`): `{id, mode: "insert", count, pageUrl, insert: { position, anchor }, placeholder: { width, height }, freeformPrompt?, screenshotPath?, comments?, strokes?}`. No `action`; requires a non-empty `freeformPrompt` **or** annotations. `placeholder` is a soft size hint.

Speed matters; the user is watching the selected element. Reuse preflight metadata, minimize discovery calls.

### Insert mode branch

1. Read the screenshot if present (annotations only).
2. If `event.scaffold` is present, use it and do **not** run the helper again. Otherwise:

```bash
node <scripts_dir>/live-insert.mjs --id EVENT_ID --count EVENT_COUNT --position after \
  --element-id "ANCHOR_ID" --classes "class1,class2" --tag "section" --text "ANCHOR_TEXT"
```

`--position` ← `event.insert.position`; anchor flags map exactly like wrap's. The scaffold has **no** `data-impeccable-variant="original"`; variants are net-new HTML+CSS at `insertLine`. On source-preview targets the scaffold carries `sourceWritten: false` with `wrapperBlock` and `replaceEndLine < replaceStartLine` (an insertion): splice variants into `wrapperBlock` at the marker and insert at `replaceStartLine` in ONE edit, exactly as the wrap section describes. Decide the visitor mode from the surface and follow the loaded craft floor before writing net-new markup. Svelte targets follow the same component flow as wrap below (`mode: "insert"` in the manifest): each variant is a real single-root component under `componentDir` with no `data-impeccable-*` attributes; never edit the route during generation; accept splices the chosen markup into `sourceFile` mechanically. For non-Svelte targets, accept/discard removes the wrapper; the anchor is untouched.

### Replace mode (default)

### 1. Read the screenshot (if present)

`event.screenshotPath` is sent **only when the user annotated before Go**; it is a PNG of the element with annotations baked in. Read it before planning. When absent, do not ask for one or screenshot the page yourself: without annotations a screenshot anchors you on the existing design and fights the three-distinct-directions brief; work from `element.outerHTML`, the computed styles, and the prompt.

Annotation semantics: a comment's `{x, y}` is element-local and binds the text to the child under that point (a comment near the title is about the title). Comments and strokes are independent unless clearly paired. Strokes read by shape: closed loop = "this thing" (emphasis, not a clipping region); arrow = direction or movement; cross/slash = delete; scribble = emphasis or delete by context. If a stroke's intent is genuinely ambiguous and it changes the brief, ask one short question before generating; otherwise state your reading in one sentence.

### 2. Wrap the element

When `event.scaffold` is present, the helper already found the source and computed the wrapper; treat it as the successful output and skip the command. `event.scaffoldAttempted` with `scaffoldError` means preflight could not finish; use the command below.

**On source-preview targets `event.scaffold` carries `sourceWritten: false`.** The helper did NOT write the wrapper; it hands you `scaffold.wrapperBlock` plus the picked element's source range (`replaceStartLine`, `replaceEndLine`, 1-indexed). Write the wrapper **and** all variants in ONE edit: splice your variants into `wrapperBlock` at the "Variants: insert below this line" marker, then replace lines `[replaceStartLine, replaceEndLine]` with the result. A separate scaffold write reloads the framework before your variant write lands and strands the browser at 0/N. (`replaceEndLine < replaceStartLine` means insert mode: insert, remove nothing.) The `svelte-component` path never sets `sourceWritten`.

```bash
node <scripts_dir>/live-wrap.mjs --id EVENT_ID --count EVENT_COUNT --element-id "ELEMENT_ID" --classes "class1,class2" --tag "div" --text "TEXT_SNIPPET"
```

Flag mapping (keep separate, never collapse into `--query`): `--element-id` ← `event.element.id`; `--classes` ← classes joined with commas; `--tag` ← tagName; `--text` ← first ~80 chars of textContent, **every call**: it disambiguates repeated sibling components, without it wrap lands on the first match. If `event.pageUrl` implies the file, pass `--file PATH`. If `--text` still matches several candidates, wrap exits `{ error: "element_ambiguous", candidates, fallback: "agent-driven" }`: pick the right range from page context and write the wrapper manually per the fallback flow.

Success output: `{ file, insertLine, commentSyntax, styleMode, styleTag, cssSelectorPrefixExamples, cssAuthoring }` (plus the `sourceWritten: false` fields above on source-preview targets). Run directly with no preflight scaffold, it writes the wrapper itself and you splice variants at `insertLine`. `styleMode` controls how preview CSS must be authored. Treat it as a detected capability mode, not a framework guess: `scoped` means `@scope ([data-impeccable-variant="N"])` rules; `astro-global-prefixed` means explicit `[data-impeccable-variant="N"]` prefixes with the exact returned `styleTag`. Use `cssAuthoring` as the source of truth for the current file (styleTag, selector strategy, requirements, forbidden patterns); apply no framework-specific exception unless it says to.

For Svelte/SvelteKit targets, `live-wrap.mjs` returns `previewMode: "svelte-component"` with `file` pointing at a temporary `node_modules/.impeccable-live/<id>/manifest.json`, `componentDir` holding the variant components, and `sourceFile` the real route. The scaffold is AST-based: control-flow blocks (`{#each}`, `{#if}`) survive intact and a free each-collection crosses the contract as ONE structured prop (kind `collection`). The payload includes `componentStubMarkup` (the prop-substituted markup already written into every stub), so do not read the manifest or stubs back. EDIT `v1.svelte`, `v2.svelte`, ... in place; never delete and recreate them; keep the stub's control flow and `propContract` prop names; never flatten a loop into literal items. The stub `<style>` arrives seeded with the source rules that currently style the selection; restyle or delete them freely. On accept, any seeded rule your variant does not re-declare is REMOVED from the source (the preview never applied it, so the user approved a design without it). Use semantic class selectors, no `@scope`, no `data-impeccable-*`. Reply with `--file` set to the manifest path; the browser mounts the compiled components so Svelte HMR does not reset page state. Accept merges the chosen component back mechanically (markup restored to route expressions, CSS reconciled, params baked, indentation preserved); you have no post-accept cleanup on this path. When the selection contains constructs a detached preview cannot support (component tags, `bind:`/`use:`, await blocks, inline scripts, spread attributes), wrap returns the normal source-preview wrapper with `previewFallback: { from: "svelte-component", reason }`; just follow the returned shape.

**Params on component-preview paths go in a sidecar, never as an attribute** (Svelte parses `{` in attribute values as an expression). Declare them in `componentDir/params.json` keyed by variant number, using the schema from section 7:

```json
{ "1": [ {"id":"density","kind":"steps","default":"snug","label":"Density","options":[
    {"value":"airy","label":"Airy"},{"value":"snug","label":"Snug"} ]} ] }
```

Author the component `<style>` against `var(--p-<id>, default)` for `range`/`toggle` and `[data-p-<id>="…"]` for `steps`, wrapped in `:global(...)` so runtime knob values on the mounted root reach your rules.

**Fallback errors.** Wrap refuses to write into non-source files (generated, untracked): accepting into one is silent data loss. Three shapes, all with `fallback: "agent-driven"` (see **Handle fallback**): `file_is_generated` (your `--file` points at a generated file), `element_not_in_source` with `generatedMatch` (element only exists generated), `element_not_found` (likely runtime-injected).

### 3. Load the action's reference

`event.action` is `impeccable` (freeform): work from `SKILL.md`'s design rules plus the loaded craft floor; decide the visitor mode from the surface; do not load a sub-command reference. Freeform is not a pass to skip parameters: follow the budget and freeform bias in section 7. For any other action (`bolder`, `quieter`, `distill`, `polish`, `typeset`, `colorize`, `layout`, `adapt`, `animate`, `delight`, `overdrive`), call `get_skill_file(name="impeccable", path="references/<action>.md")` on `gobby-skills` before planning; its MUST params layer on top of the section 7 budget.

### 4. Plan three variants: identity first, then mode, then axes

Live runs on an existing surface; the brand is already chosen. The job is variation **within identity**, not selection between identities. The worst failure is three off-brand variants the user cannot accept. Four phases, in order.

#### Phase A: Extract the identity (non-skippable)

Sources in priority order: `.impeccable.md`'s visual-system fields; CSS custom properties (de-facto tokens); computed styles on the picked element and parent; sibling components' visual rhetoric. Write ONE sentence recording what is actually on screen: dominant surface and accent color (real values, not "warm"), the loaded font pairing, layout topology (stacked / side-by-side / grid / asymmetric / overlay), surface treatment (corners, borders, shadows, decoration density), and the voice tone read off the copy. Be specific; skip an axis rather than fabricate; do not name an aesthetic family (a conclusion, not data). This sentence is the **identity lock**: every variant must read as the same brand side by side. Absence of `.impeccable.md` is never an excuse.

#### Phase B: Pick mode (default vs departure)

**Default** preserves the identity and varies expression within it; right for ~90% of sessions. **Departure** rejects the identity; trigger ONLY on the user's explicit ask in the current request or prompt ("redesign this", "rebuild from scratch", "something completely different"); a stale critique or old note is not authorization. Unsure means default: wrong-default costs "three on-brand variants with similar feel" (recoverable), wrong-departure costs three off-brand variants (unrecoverable).

#### Phase C: Plan three variants

**Default mode.** Each variant commits to a different **primary axis**, preserving the identity sentence. The six axes: 1 **Hierarchy** (which element commands the eye), 2 **Layout topology** (stacked / side-by-side / grid / asymmetric / overlay), 3 **Typographic system** (pairing logic, scale ratio, case/weight, *within the available faces*), 4 **Color strategy** (which existing palette role carries the surface: Restrained / Committed / Full palette / Drenched; existing tokens only), 5 **Density** (minimal / comfortable / dense), 6 **Structural decomposition** (merge, split, progressive disclosure). Three variants, three DIFFERENT axes: the same brand at three angles. New fonts, new hues, or new aesthetic-family signals belong to departure mode only.

**Departure mode.** Each variant anchors to a different aesthetic direction derived from the brand, never a fixed catalog: read `.impeccable.md`'s brand personality and voice; derive physical, spatial, or material experiences that embody them; from those, derive three directions genuinely different from each other AND from the current surface; reject reflex choices whose rationale would fit a neighboring product. Each direction must be one concrete sentence naming a real-world referent ("a museum exhibition label system", not "clean and minimal").

**In both modes, name each variant's 2 or 3 parameter knobs while planning** (section 7 budget). Parameters are part of the design; deciding "what's tunable" during planning beats retrofitting.

#### Phase D: Squint test

**Default:** compare each variant against the Phase A lock; palette, type voice, or rhetoric drift means it crossed into departure by accident: rework. Then confirm three different primary axes; three "tighter density" variants is failure. **Departure:** two passes, family before sentence. Family pass (non-negotiable): label each variant with a concrete family of your own choosing; shared or interchangeable labels mean rework. Sentence pass: three one-line descriptions side by side; two that rhyme mean rework. When the primary axis is color or theme, the trio must not share theme + dominant hue: three color worlds, not three shades.

**Action-specific invocations** must vary along the action's dimension:

- `bolder`: amplify a different dimension per variant (scale / saturation / structural change).
- `quieter`: pull back a different dimension (color / ornament / spacing).
- `distill`: remove a different class of excess (visual noise / redundant content / nested structure).
- `polish`: a different refinement axis (rhythm / hierarchy / micro-details).
- `typeset`: different pairing AND different scale ratio each.
- `colorize`: different hue family each; vary chroma and contrast strategy.
- `layout`: different structural arrangement, not spacing tweaks.
- `adapt`: different target context per variant (mobile-first / tablet / desktop / print or low-data).
- `animate`: different motion vocabulary (cascade stagger / clip wipe / scale-and-focus / morph / parallax).
- `delight`: different flavor of personality (micro-interaction / typographic surprise / illustrated accent / sonic-or-haptic / easter egg).
- `overdrive`: different convention broken (scale / structure / motion / input model / state transitions); skip its "propose and ask" step, live is non-interactive.

### 5. Apply the freeform prompt (if present)

`event.freeformPrompt` is the user's ceiling on direction: all variants honor it while exploring different interpretations within the Phase B mode. Default mode: the prompt narrows the axes, not the identity ("more confident" → one variant amplifies hierarchy, one commits the accent color, one tightens density). Departure mode: the prompt narrows the lanes, not the families ("newspaper front page" → broadsheet vs tabloid vs trade journal, then run the family pass). When the prompt conflicts with a binding brand commitment or `.impeccable.md` invariant, preserve the invariant unless the user explicitly revokes it.

After planning and applying the prompt, call `get_skill_file(name="impeccable", path="references/live-variants.md")` to deliver variants, declare parameters, and signal completion.
