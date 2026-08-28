---
name: impeccable
description: "Create distinctive, production-grade frontend interfaces with high design quality. Generates creative, polished code that avoids generic AI aesthetics. Use when the user asks to build web components, pages, artifacts, posters, or applications, or when any design skill requires project context. Call with 'craft' for shape-then-build, 'teach' for design context setup, or 'extract' to pull reusable components and tokens into the design system."
version: "1.0.0"
category: frontend
triggers: impeccable, design system, frontend design, build page, create component, web ui, landing page, dashboard, beautify, style page, design interface, design context, extract components
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
    runtime:
      node: ">=22.12.0"
      cli:
        npm: "impeccable"
        version: "3.5.0"
        bin: "impeccable"
      skill_release: "4.0.4"
---

<!--
Impeccable — Copyright 2025-2026 Paul Bakaus. Licensed under Apache 2.0.
Based on Anthropic's frontend-design skill (Copyright 2025 Anthropic, PBC, Apache 2.0).
See NOTICE.md in this directory for attribution.

Upstream: https://github.com/pbakaus/impeccable (v3.5.0, commit a075d89b)
The `impeccable` skill ships with all 39 released 4.0.4 reference files plus
seven Gobby-retained domain references under `references/`, and the full 4.0.4
script release under `scripts/`.
The dispatch table below loads references via
`get_skill_file(name="impeccable", path="references/<cmd>.md")` on
`gobby-skills`; scripts run from the cache directory returned by
`materialize_skill_scripts(name="impeccable")`.
-->

This skill guides creation of distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implement real working code with exceptional attention to aesthetic details and creative choices.

## Context Gathering Protocol

Design skills produce generic output without project context, so confirm design context before doing any design work.

**Required context** (every design skill needs at minimum):
- **Target audience**: Who uses this product and in what context?
- **Use cases**: What jobs are they trying to get done?
- **Brand personality/tone**: How should the interface feel?

Individual skills may require additional context. Check the skill's preparation section for specifics.

This context cannot be inferred by reading the codebase — code tells you what was built, not who it's for or what it should feel like. Only the creator can provide it.

**Gathering order:**
1. **Check current instructions (instant)**: If your loaded instructions already contain a **Design Context** section, proceed immediately.
2. **Check .impeccable.md (fast)**: If not in instructions, read `.impeccable.md` from the project root. It is the project's design contract — audience, tokens, color and contrast constraints, canonical components, per-surface rules — and this skill is written to be used with it, not instead of it. If it exists and contains the required context, proceed.
3. **Run impeccable teach**: If neither source has context, run the impeccable skill in `teach` mode before anything else — inferring context from the codebase instead produces exactly the generic output this skill exists to prevent.

## Visitor Modes

Every surface serves one visitor mode; name it before designing and let it steer which rules bind:

- **Persuade** — marketing pages, landing pages, launch moments. The visitor is deciding; commit to a bold visual world. (Gobby: the gobby.ai marketing site.)
- **Operate** — product UI where the user is in a task: app screens, dashboards, settings, tables, tools. Earned familiarity beats novelty. Load `references/operate.md` for extended depth. (Gobby: the `web/` product UI, installer, CLI/TUI surfaces.)
- **Read** — docs, guides, long-form. Prose measure and navigation dominate; take the typography and consistency rules from `references/operate.md`. (Gobby: docs and wiki reading surfaces.)
- **Experience** — playable or expressive pages where the visit itself is the product. The rarest mode; everything can be committed.

**Quality floor**: before editing any surface in any mode, load `references/craft-floor.md` via `get_skill_file` — its Verify and Refuse lists are the floor every mode builds on. The steering references assume it.

---

## Sub-command Dispatch

This skill is a **router** over 23 user arguments backed by 25 reference files, plus reference-backed `teach` mode. Evaluate the argument passed after `/gobby impeccable` and take the matching action below. With no argument, call `get_skill_file(name="impeccable", path="references/routing.md")` on `gobby-skills` and follow its context-aware menu flow.

### Inline mode

| Argument | What it does | See section |
|----------|--------------|-------------|
| `teach` | Create or refresh the project design contract in `.impeccable.md` | `references/teach.md` |

For `teach`, call `get_skill_file(name="impeccable", path="references/teach.md")` and follow it.

### Reference-backed flows

When the argument matches a row below, call `get_skill_file(name="impeccable", path="<reference>")` on `gobby-skills` and follow the returned instructions. Treat remaining words as the operation target. For `audit` and `adapt`, choose the native variant for native applications and the general variant for other surfaces.

| Argument | Purpose | Reference |
|----------|---------|-----------|
| `craft` | Shape-then-build alias using normal dispatch | `references/craft.md` |
| `shape` | Plan UX/UI before writing code | `references/shape.md` |
| `init` | Route design-context setup to teach mode | `references/init.md` |
| `document` | Maintain the project design contract | `references/document.md` |
| `extract` | Consolidate reusable components and tokens | `references/extract.md` |
| `critique` | Run an adversarial multi-persona review | `references/critique.md` |
| `audit` | Review web or native quality | `references/audit.md`; native: `references/audit.native.md` |
| `polish` | Run a final detail pass | `references/polish.md` |
| `bolder` | Increase aesthetic intensity | `references/bolder.md` |
| `quieter` | Reduce aesthetic intensity | `references/quieter.md` |
| `distill` | Strip a design to its essentials | `references/distill.md` |
| `harden` | Cover edge states, i18n, overflow, and errors | `references/harden.md` |
| `onboard` | Improve time-to-value and onboarding | `references/onboard.md` |
| `live` | Run the agent-driven browser variant loop | `references/live.md` |
| `animate` | Add purposeful motion | `references/animate.md` |
| `colorize` | Rework the color system | `references/colorize.md` |
| `typeset` | Refine typography and type scale | `references/typeset.md` |
| `layout` | Rework composition and hierarchy | `references/layout.md` |
| `delight` | Add targeted interaction charm | `references/delight.md` |
| `overdrive` | Apply a maximalist creative push | `references/overdrive.md` |
| `clarify` | Improve UX writing and hierarchy | `references/clarify.md` |
| `adapt` | Adapt across responsive or native contexts | `references/adapt.md`; native: `references/adapt.native.md` |
| `optimize` | Improve performance and payload | `references/optimize.md` |

### Supporting references

- New or replacement visual work loads `references/new-work.md`, which loads `references/visualize.md` when image generation is available.
- Native flows load `references/ios.md` and/or `references/android.md` before platform-specific adaptation or audit.
- Live mode loads `references/live-setup.md` only for one-time configuration, drift handling, or CSP consent.
- Hook documentation and lifecycle diagnostics load `references/hooks.md` and `references/doctor.md` directly.
- Degraded in-thread fallbacks remain reachable with `get_skill_file` at `references/degraded/asset-producer.md`, `references/degraded/documenter.md`, `references/degraded/finish-reviewer.md`, and `references/degraded/manual-edit-applier.md`.

### Fallback

- If the argument does not match the inline mode or a reference-backed flow, tell the user the argument was not recognized and show the menu from `references/routing.md`.
- If `get_skill_file` returns `{"success": false, ...}`, surface the error and show the menu again.
- If no argument was provided, use `references/routing.md`; do not silently proceed into design work.

### Bundled scripts

The skill bundles the full 4.0.4 Node script tree under `scripts/`, synced into
the skill-files registry like every other skill file. Never run scripts from
this skill's source tree — installed skills may have no on-disk tree at all.
Resolve a runnable copy first:

1. Call `materialize_skill_scripts(name="impeccable")` on `gobby-skills`. It
   writes the canonical `scripts/**` bytes from the registry into a
   content-addressed cache, installs the locked dependencies, and returns
   `scripts_dir` plus `environment.PUPPETEER_CACHE_DIR`.
2. Run entry points from there via Bash, e.g.
   `node <scripts_dir>/detect.mjs --json <file-or-dir>` (domain filters:
   `--scope type`, `--scope layout`) or
   `node <scripts_dir>/critique-storage.mjs latest <target>`.
3. Critique snapshots write to `.impeccable/critique/` in the project
   (gitignored).

Export the returned `environment.PUPPETEER_CACHE_DIR` before invoking a browser engine.
If Node or the tool is unavailable, skip detector runs and scan manually —
never block design work on the detector.

## Shared Invariants

- Establish the project design contract before design work.
- Name the visitor mode and load `references/craft-floor.md` before editing.
- Preserve accessibility, responsive behavior, real functionality, and the established visual world.
- Use the selected mode router; avoid loading unrelated mode guidance.

For aesthetic direction, typography, or color, call `get_skill_file(name="impeccable", path="references/design-foundations.md")`. For layout, motion, interaction, responsive behavior, UX writing, the AI-slop test, or implementation principles, call `get_skill_file(name="impeccable", path="references/design-execution.md")`. Load only the reference required by the selected operation. Normal modes stay within three focused references.
