# Notice

Impeccable
Copyright 2025-2026 Paul Bakaus

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
this file except in compliance with the License. You may obtain a copy of the
License at:

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.

## Upstream

- Source: https://github.com/pbakaus/impeccable
- License: Apache License 2.0
- Copyright: 2025-2026 Paul Bakaus
- Vendored version: v3.5.0, commit `a075d89b` (re-vendored 2026-08-05; original
  vendor 2026-04-15 from `f589ff0b6`)

Upstream was rearchitected between our vendors: skill content now lives in
`skill/` (`SKILL.src.md` + `reference/`) and is compiled into per-harness
generated output (e.g. `.claude/skills/impeccable/`). Gobby vendors:

- The 17 steering-command references from `skill/reference/<cmd>.md`, shipped
  as `references/<cmd>.md`. The main `SKILL.md` acts as a router dispatching to
  them via `get_skill_file(name="impeccable", path="references/<cmd>.md")` on
  `gobby-skills`.
- The new `skill/reference/craft-floor.md` (quality floor: Verify + Refuse
  lists) and `skill/reference/operate.md` (Operate/Read mode depth) as
  `references/craft-floor.md` and `references/operate.md`.
- The runnable script subset under `scripts/` — taken from the **generated**
  skill output at `.claude/skills/impeccable/scripts/` at the same commit (the
  generated tree is the dependency-free compiled form). See "Vendored scripts"
  below.

Gobby keeps its own architecture: markdown references served by
`get_skill_file`, project design context in `.impeccable.md` (written by teach
mode), and script execution from a materialized cache. Upstream's
CLI-orchestrated product machinery is deliberately not vendored (see "Not
vendored").

## Modifications from upstream

Main skill (`SKILL.md`):
- Frontmatter rewritten to the Gobby shared-skill format.
- `## Sub-command Dispatch` router section retained (Gobby-specific).
- New `## Visitor Modes` section added, adapting upstream's
  Persuade/Operate/Read/Experience model with Gobby surface mappings (product
  UI = Operate, gobby.ai marketing = Persuade, docs = Read) and routing
  `references/craft-floor.md` as the pre-edit quality floor.
- New `### Bundled detector and critique scripts` section documenting the
  `materialize_skill_scripts` execution flow.
- `reference/` renamed `references/` (plural) per the Gobby loader convention.

Standard adaptations applied to every refreshed reference
(`references/<cmd>.md`, 17 files, from upstream `a075d89b`):
- Upstream's `> **Additional context needed**: X.` opening blockquote merged
  into the standard session-continuation preamble ("You are continuing a
  session under the `impeccable` skill; …"); files without one get the
  preamble prepended.
- First prose mention of another steering command expanded to the
  `get_skill_file` load instruction; later mentions stay bare backticked names;
  fenced code blocks untouched.
- `{{scripts_path}}` → `<scripts_dir>`, with a resolver paragraph after the
  first script invocation per file: resolve via
  `materialize_skill_scripts(name="impeccable")` on `gobby-skills`; degrade to
  a manual scan when Node or the tool is unavailable.
- `DESIGN.md` → `.impeccable.md` (the project design contract).
- Placeholders: `{{available_commands}}` → backticked 17-command list;
  `{{command_prefix}}x` → bare `x` in code spans / expanded form in prose;
  `{{ask_instruction}}` → "ask the user" (or structured-question-tool wording);
  `{{config_file}}` → `.impeccable.md`.
- References to non-vendored machinery (native variants, live mode, hooks,
  doctor, `new-work.md`, `context.mjs`, PRODUCT.md artifacts) dropped or
  rerouted to the nearest vendored equivalent.

Per-file notes beyond the standard adaptations:
- `adapt.md`: dropped the native-platform routing paragraph; noted upstream
  inlined its former responsive-design material while Gobby retains the
  standalone `references/responsive-design.md`.
- `animate.md`, `typeset.md`, `layout.md`: dropped the **Native** visitor-mode
  bullet (points to non-vendored `ios.md`/`android.md`).
- `audit.md`: dropped the `audit.native.md` routing line; bundled-detector
  prose made concrete as `node <scripts_dir>/detect.mjs --json <target>` with
  the resolver paragraph.
- `colorize.md`: "use new-work.md for a new identity" → confirm the direction
  with the user instead; dropped the `## Live-mode signature params` section.
- `critique.md`: dropped all 7 upstream `<codex>` harness blocks; live-overlay
  injection flow rewritten as a plain browser-inspection flow (live-server is
  not vendored) and report wording adjusted ("Browser evidence"); kept
  `.impeccable/critique/` storage, the degraded-banner protocol, and the full
  inline persona/heuristic material verbatim.
- `layout.md`: `new-work.md` identity-replacement routing → the `craft` flow;
  dropped the `## Live-mode signature params` section.
- `shape.md`: two `new-work.md` removals — interview note and Phase-2 routing
  now point at the main skill's Design Direction guidance and `.impeccable.md`.
- `typeset.md`: identity-replacement sentence rewritten to route through
  teach-mode updates of `.impeccable.md`; dropped the live-mode section.
- `polish.md`: §5 `context.mjs`/hooks sentence rewritten to run the bundled
  detector once, with manual scan only when no detector is available.
- `overdrive.md`: kept the OVERDRIVE banner and browser-automation iteration
  section verbatim.
- `craft-floor.md` (new): dropped upstream's `<codex>` and `<gemini>`
  calibration blocks; the design-hook sentence rewritten to reference the
  bundled detector; upstream `rule:` anchor comments retained.
- `operate.md` (new): near-verbatim; the `craft-floor.md` link expanded to the
  `get_skill_file` load instruction.

Domain references (`references/typography.md`, `color-and-contrast.md`,
`motion-design.md`, `spatial-design.md`, `interaction-design.md`,
`responsive-design.md`, `ux-writing.md`): upstream deleted these at v3.5.0
(content folded into craft-floor and the rewritten commands). Gobby retains
them unchanged as still-accurate depth; `.impeccable.md` references their
material.

Craft-mode references (`references/craft.md`, `references/extract.md`): kept
from the original vendor (upstream's inline-mode flows), with the Step-1
`get_skill_file` routing adaptation.

## Vendored scripts

`scripts/` contains 32 files copied **unmodified** from the generated skill
output at `.claude/skills/impeccable/scripts/` at commit `a075d89b`:

- `detect.mjs` — anti-pattern detector CLI entry point.
- `detector/` — the full detector tree (20 files: CLI, registry, rules,
  regex/static-html/browser/visual engines, shared helpers, browser-inject
  bundle).
- `critique-storage.mjs` — critique snapshot persistence
  (slug/write/latest/trend); snapshots live in `.impeccable/critique/`
  (gitignored in the Gobby repo).
- `lib/` — the exact static import closure of the above (9 files:
  `artifact-schema`, `impeccable-config`, `impeccable-paths`, `provider`,
  `staleness-notice`, `staleness`, `surface-briefs`, `target-args`,
  `target-slug`).
- `context.mjs` — vendored **solely** as a static library dependency of
  `lib/impeccable-paths.mjs` (`resolveProjectRoot`). It is main-module guarded
  (importing it has no side effects); its CLI/setup workflow is not part of the
  Gobby flow.

Properties and rules:
- Zero npm dependencies. Optional capabilities are lazily imported and degrade
  gracefully when absent: `puppeteer` (URL scanning), `htmlparser2`/
  `css-select`/`css-tree`/`domutils` (full static-HTML parsing). Without them
  the detector falls back to line-based text scanning — multi-line CSS rule
  correlations may be missed, and most layout-scope rules require the browser
  or parser engines.
- Never hand-edit these files. Refresh wholesale from the upstream generated
  output at a newer pinned commit.
- Execution model: scripts are synced into the skill-files registry and run
  from the content-addressed cache returned by
  `materialize_skill_scripts(name="impeccable")` — never from this source tree
  (installed skills may have no disk tree).

## Not vendored

Upstream product machinery tied to its CLI-orchestrated PRODUCT.md/DESIGN.md
artifact model is deliberately not vendored: live mode (`live-*`,
`live.md`, `live-setup.md`), design-detector hooks (`hook-*`, `hooks.md`),
`doctor`, `context.mjs` as a workflow (see above), `concept-seed`,
`serve-question`, `generate-image`, `palette`, `pin`, `surface-brief`,
`embed-prompt`, `context-signals`, `detect-csp`, `modern-screenshot.umd.js`,
native variants (`ios.md`, `android.md`, `adapt.native.md`,
`audit.native.md`), and the `init`/`onboard`/`routing`/`document`/
`visualize`/`new-work` references. `new-work.md` should be reconsidered when
gobby.ai marketing-site work starts.

## Anthropic frontend-design Skill

The `impeccable` skill in the upstream project builds on Anthropic's original
frontend-design skill.

- Original work: https://github.com/anthropics/skills/tree/main/skills/frontend-design
- Original license: Apache License 2.0
- Copyright: 2025 Anthropic, PBC

The upstream project extends the original with domain-specific reference files,
steering commands, and expanded patterns and anti-patterns. See the upstream
repo's `NOTICE.md` for the full attribution chain.
