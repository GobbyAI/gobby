# Live variant delivery

Before running any `node <scripts_dir>` command, use
`materialize_skill_scripts(name="impeccable")` and set
`environment.PUPPETEER_CACHE_DIR` from its result.

### 6. Deliver variants

Complete HTML replacement of the original element per variant, not a CSS-only patch. Colocate preview CSS as a `<style>` tag inside the wrapper. **Atomic default:** CSS + all variants + parameter manifests in one edit at `insertLine`.

```html
<!-- Variants: insert below this line -->
<style data-impeccable-css="SESSION_ID">
  /* rules matching cssAuthoring.rulePattern */
</style>
<div data-impeccable-variant="1">
  <!-- variant 1: full element replacement (single top-level element) -->
</div>
<div data-impeccable-variant="2" style="display: none">
  <!-- variant 2 -->
</div>
<div data-impeccable-variant="3" style="display: none">
  <!-- variant 3 -->
</div>
```

Replace the style opening tag with `cssAuthoring.styleTag` when the tool returns a different one. **Each variant div contains exactly one top-level element**, same tag as the original; loose siblings break outline tracking and accept. First variant visible, all others `display: none`. The browser's MutationObserver accepts atomic or progressive arrival; accepting an arrived variant fences the worker, so later publications are rejected.

For `styleMode: "scoped"`, author every `:scope` rule with a descendant combinator: the `@scope` boundary is the variant wrapper div, not your element, so a bare `:scope { ... }` styles a `display: contents` shell. Always step in (`:scope > .card`, `:scope .hero-title`). The fake test agent's CSS in `tests/live-e2e/agent.mjs` is a faithful template.

**JSX / TSX targets:** wrap `<style>` content in a template literal (CSS braces would parse as JSX), use `className=` / `style={{…}}`, keep `data-impeccable-*` attributes as plain strings:

```tsx
<style data-impeccable-css="SESSION_ID">{`
  @scope ([data-impeccable-variant="1"]) { ... }
`}</style>
<div data-impeccable-variant="2" style={{ display: 'none' }}>
  {/* variant 2 */}
</div>
```

The wrap script provides a single-rooted JSX wrapper with the marker comments inside; drop the block at the marker and the source stays valid TSX.

### 7. Parameters (composition-sized, 0-4 per variant)

Each variant can expose **coarse** knobs; the browser docks one control per parameter with zero regeneration cost (knobs drive a CSS variable or data attribute your scoped CSS is authored against). Wire an axis as soon as the user could plausibly mutter "a bit tighter" or "a touch more accent" without wanting a regeneration; micro-margins and one-off nudges are not parameters. Freeform bias: you chose the axes, so expose them; a hero with 0 params is almost always a mistake, and 1 is underweight unless the design is a genuine fixed point.

Budget scales with the element's VISUAL weight (count visual children, not DOM depth):

- **Leaf / tiny** (button, icon, bare heading): **0 params.**
- **Small composition** (simple card, labeled input, ≤ ~5 visual children): **0-1**.
- **Medium composition** (section, nav cluster, 6-15 children): **target 2**; 1 if simple.
- **Large composition** (hero, full region, 16+ children or sub-sections): **target 2-3, up to 4** when independent axes are all authored in CSS.

**Hard cap: four** per variant. For named sub-commands, the action reference's MUST params are non-negotiable when expressible; respect the cap, no duplicate knobs.

**Declare** on the HTML/JSX path as a wrapper attribute (component-preview paths use `componentDir/params.json` instead, same schema, keyed by variant number; see the wrap section):

```html
<div data-impeccable-variant="1" data-impeccable-params='[
  {"id":"color-amount","kind":"range","min":0,"max":1,"step":0.05,"default":0.5,"label":"Color amount"},
  {"id":"serif","kind":"toggle","default":false,"label":"Serif display"}
]'>
```

Three kinds: `range` (slider; drives `--p-<id>`; author `var(--p-color-amount, 0.5)`; fields min/max/step/default/label), `steps` (segmented radio; drives `data-p-<id>`; author `:scope[data-p-density="airy"] .grid { ... }`; fields options/default/label), `toggle` (drives both `--p-<id>: 0|1` and attribute presence; fields default/label). Reset on variant switch is a known limitation: each variant starts at its declared defaults.

**On accept**, the browser sends current values and `live-accept.mjs` writes them as a sibling comment: `<!-- impeccable-param-values SESSION_ID: {"color-amount":0.7} -->`. Carbonize cleanup bakes them: keep only the matching `steps`/`toggle` branch, drop the others, collapse `:scope[data-p-…]` to semantic rules; substitute `range` literals or update the var's default.

### 8. Signal done

```bash
node <scripts_dir>/live-poll.mjs --reply EVENT_ID done --file RELATIVE_PATH
```

`RELATIVE_PATH` is relative to project root; the browser fetches source directly if the dev server lacks HMR. Then poll again immediately.
