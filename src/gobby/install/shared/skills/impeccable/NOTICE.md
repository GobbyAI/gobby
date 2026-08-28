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
- Reference adaptation baseline: commit `a075d89b` (refreshed 2026-08-05).
- Script release: 4.0.4, staged 2026-08-08 through the release channel with
  Gobby's pinned `impeccable@3.5.0` CLI.
- Original vendor: 2026-04-15 from commit `f589ff0b6`.

Upstream skill content lives in `skill/` (`SKILL.src.md` + `reference/`) and is
compiled into per-harness generated output. Gobby vendors all 39 released
reference files from the generated 4.0.4 output and retains seven domain
references removed upstream at v3.5.0.

The main `SKILL.md` routes reference-backed flows through
`get_skill_file(name="impeccable", path="references/<path>.md")` on
`gobby-skills`. Gobby also vendors:

- The full released script tree under `scripts/` — taken from the **generated**
  4.0.4 skill output at `.claude/skills/impeccable/scripts/`. See "Vendored
  scripts" below.

Gobby keeps its own architecture: markdown references served by
`get_skill_file`, project design context in `.impeccable.md` (written by teach
mode), Gobby-owned installation and updates, and script execution from a
materialized cache.

## Modifications from upstream

### Reference catalogue

| Reference | Classification |
|-----------|----------------|
| `references/craft-floor.md` | Refreshed with catalogued adaptations |
| `references/operate.md` | Refreshed with catalogued adaptations |
| `references/adapt.md` | Refreshed with catalogued adaptations |
| `references/animate.md` | Refreshed with catalogued adaptations |
| `references/audit.md` | Refreshed with catalogued adaptations |
| `references/bolder.md` | Refreshed with catalogued adaptations |
| `references/clarify.md` | Refreshed with catalogued adaptations |
| `references/colorize.md` | Refreshed with catalogued adaptations |
| `references/critique.md` | Refreshed with catalogued adaptations |
| `references/delight.md` | Refreshed with catalogued adaptations |
| `references/distill.md` | Refreshed with catalogued adaptations |
| `references/harden.md` | Refreshed with catalogued adaptations |
| `references/layout.md` | Refreshed with catalogued adaptations |
| `references/optimize.md` | Refreshed with catalogued adaptations |
| `references/overdrive.md` | Refreshed with catalogued adaptations |
| `references/polish.md` | Refreshed with catalogued adaptations |
| `references/quieter.md` | Refreshed with catalogued adaptations |
| `references/shape.md` | Refreshed with catalogued adaptations |
| `references/typeset.md` | Refreshed with catalogued adaptations |
| `references/init.md` | Named-default adaptation: routes to teach mode |
| `references/document.md` | Named-default adaptation: maintains `.impeccable.md` |
| `references/hooks.md` | Named-default adaptation: documentation only; Gobby rules own enforcement |
| `references/routing.md` | Named-default adaptation: uses `SKILL.md` dispatch |
| `references/craft.md` | Named-default adaptation: alias uses normal dispatch |
| `references/new-work.md` | Named-default adaptation: `.impeccable.md` and teach own visual authority |
| `references/live.md` | Named-default adaptation: agent-driven live mode in ordinary Gobby sessions |
| `references/live-setup.md` | Named-default adaptation: materialized-script setup without daemon management |
| `references/onboard.md` | Standard adaptation |
| `references/visualize.md` | Standard adaptation |
| `references/extract.md` | Standard adaptation |
| `references/doctor.md` | Standard adaptation: Gobby lifecycle diagnostics |
| `references/ios.md` | Near-verbatim native reference |
| `references/android.md` | Near-verbatim native reference |
| `references/adapt.native.md` | Near-verbatim native reference |
| `references/audit.native.md` | Near-verbatim native reference |
| `references/degraded/asset-producer.md` | Vendored as-is |
| `references/degraded/documenter.md` | Vendored as-is |
| `references/degraded/finish-reviewer.md` | Vendored as-is |
| `references/degraded/manual-edit-applier.md` | Vendored as-is |
| `references/color-and-contrast.md` | Gobby-retained upstream domain reference |
| `references/critique-cognitive-load.md` | Gobby-retained decomposition reference |
| `references/critique-personas.md` | Gobby-retained decomposition reference |
| `references/critique-report.md` | Gobby-retained decomposition reference |
| `references/critique-scoring.md` | Gobby-retained decomposition reference |
| `references/critique-workflow.md` | Gobby-retained decomposition reference |
| `references/design-execution.md` | Gobby-retained decomposition reference |
| `references/design-foundations.md` | Gobby-retained decomposition reference |
| `references/interaction-design.md` | Gobby-retained upstream domain reference |
| `references/live-actions.md` | Gobby-retained decomposition reference |
| `references/live-contract.md` | Gobby-retained decomposition reference |
| `references/live-generation.md` | Gobby-retained decomposition reference |
| `references/live-setup-recovery.md` | Gobby-retained decomposition reference |
| `references/live-variants.md` | Gobby-retained decomposition reference |
| `references/motion-design.md` | Gobby-retained upstream domain reference |
| `references/new-work-build.md` | Gobby-retained decomposition reference |
| `references/new-work-direction.md` | Gobby-retained decomposition reference |
| `references/new-work-finish.md` | Gobby-retained decomposition reference |
| `references/new-work-invention.md` | Gobby-retained decomposition reference |
| `references/responsive-design.md` | Gobby-retained upstream domain reference |
| `references/spatial-design.md` | Gobby-retained upstream domain reference |
| `references/teach.md` | Gobby-retained decomposition reference |
| `references/typography.md` | Gobby-retained upstream domain reference |
| `references/ux-writing.md` | Gobby-retained upstream domain reference |

The four `references/degraded/*.md` files are byte-for-byte copies of the
released 4.0.4 output. They remain reachable through `get_skill_file` for
in-thread fallback when a harness cannot run the shipped agents.

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

Initial standard adaptations applied to the 17 steering references refreshed
from upstream `a075d89b`:
- Upstream's `> **Additional context needed**: X.` opening blockquote merged
  into the standard session-continuation preamble ("You are continuing a
  session under the `impeccable` skill; …"); files without one get the
  preamble prepended.
- First prose mention of another steering command expanded to the
  `get_skill_file` load instruction; later mentions stay bare backticked names;
  fenced code blocks untouched.
- `{{scripts_path}}` → `<scripts_dir>`, with a resolver paragraph after the
  first script invocation per file: resolve via
  `materialize_skill_scripts(name="impeccable")` on `gobby-skills`, export its
  returned `environment.PUPPETEER_CACHE_DIR`, and degrade to a manual scan when
  Node or the tool is unavailable.
- `DESIGN.md` → `.impeccable.md` (the project design contract).
- Placeholders: `{{available_commands}}` → backticked 17-command list;
  `{{command_prefix}}x` → bare `x` in code spans / expanded form in prose;
  `{{ask_instruction}}` → "ask the user" (or structured-question-tool wording);
  `{{config_file}}` → `.impeccable.md`.
Per-file notes beyond the standard adaptations:
- `adapt.md`: dropped the native-platform routing paragraph; noted upstream
  inlined its former responsive-design material while Gobby retains the
  standalone `references/responsive-design.md`.
- `animate.md`, `typeset.md`, `layout.md`: the initial refresh dropped the
  inline **Native** visitor-mode bullet; native dispatch now loads the platform
  references directly.
- `audit.md`: kept the general audit web-specific; native dispatch loads
  `audit.native.md`. Bundled-detector prose uses
  `node <scripts_dir>/detect.mjs --json <target>` with the resolver paragraph.
- `colorize.md`: "use new-work.md for a new identity" → confirm the direction
  with the user instead; dropped the `## Live-mode signature params` section.
- `critique.md`: dropped all 7 upstream `<codex>` harness blocks; live-overlay
  injection flow rewritten as a plain browser-inspection flow because live mode
  was outside that refresh, and report wording adjusted ("Browser evidence"); kept
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
  bundled detector; upstream `rule:` anchor comments retained; reconciled with
  4.0.4's removal of the Browser-surfaces floor.
- `operate.md` (new): near-verbatim; the `craft-floor.md` link expanded to the
  `get_skill_file` load instruction.

Domain references (`references/typography.md`, `color-and-contrast.md`,
`motion-design.md`, `spatial-design.md`, `interaction-design.md`,
`responsive-design.md`, `ux-writing.md`): upstream deleted these at v3.5.0
(content folded into craft-floor and the rewritten commands). Gobby retains
them unchanged as still-accurate depth; `.impeccable.md` references their
material.

The 4.0.4 additions use the same preamble, cross-reference expansion,
`.impeccable.md` substitution, and materialized-script resolver where
applicable. Named ownership adaptations route `init.md` to teach mode,
`document.md` and `new-work.md` to `.impeccable.md`, `hooks.md` to
documentation-only behavior under Gobby rules, `routing.md` and `craft.md` to
the main dispatch, `doctor.md` to Gobby lifecycle diagnostics, and `live.md`
plus `live-setup.md` to agent-driven materialized scripts in ordinary Gobby
sessions. The live adaptation keeps upstream overlay cleanup and recovery,
assumes the app dev server is already running, and adds no Gobby daemon-level
port or session manager. Native references retain upstream platform guidance
with the session preamble and expanded loads. `onboard.md`, `visualize.md`, and
`extract.md` receive the standard transforms.

## Vendored scripts

`scripts/` contains all 107 files copied **unmodified** from the generated
4.0.4 release output staged at `.claude/skills/impeccable/scripts/`. The
inventory includes detector, critique, live-mode, hook, image, context, and
shared-library entry points exactly as released.

`scripts/package.json` and `scripts/package-lock.json` are Gobby additions.
They record the parser dependencies and optional Puppeteer dependency exposed
by `impeccable@3.5.0` and lock the complete materialization-time dependency
graph. They are excluded from the 107-file upstream count.

Properties and rules:
- Dependencies install only inside the content-addressed materialization cache
  with `npm ci`; browser binaries use the shared `PUPPETEER_CACHE_DIR` returned
  by the materializer.
- Never hand-edit the 107 released files. Refresh them wholesale from a staged
  release-channel artifact.
- Execution model: scripts are synced into the skill-files registry and run
  from the content-addressed cache returned by
  `materialize_skill_scripts(name="impeccable")` — never from this source tree
  (installed skills may have no disk tree).

## Integration boundary

Vendoring a released script does not install its upstream lifecycle or hook
wiring. Gobby owns skill installation, updates, rules, materialization, and
cache cleanup. References are adapted and routed separately through
`gobby-skills`.

## Anthropic frontend-design Skill

The `impeccable` skill in the upstream project builds on Anthropic's original
frontend-design skill.

- Original work: https://github.com/anthropics/skills/tree/main/skills/frontend-design
- Original license: Apache License 2.0
- Copyright: 2025 Anthropic, PBC

The upstream project extends the original with domain-specific reference files,
steering commands, and expanded patterns and anti-patterns. See the upstream
repo's `NOTICE.md` for the full attribution chain.
