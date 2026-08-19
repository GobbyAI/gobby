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

This skill is a **router** over 23 user arguments backed by 25 reference files, plus the inline `teach` mode. Evaluate the argument passed after `/gobby impeccable` and take the matching action below. With no argument, call `get_skill_file(name="impeccable", path="references/routing.md")` on `gobby-skills` and follow its context-aware menu flow.

### Inline mode

| Argument | What it does | See section |
|----------|--------------|-------------|
| `teach` | Create or refresh the project design contract in `.impeccable.md` | `## Teach Mode` |

For `teach`, skip the reference dispatch and jump to the named section below.

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

The rest of this file (below the `---` separator) is the design reference that grounds every mode and steering command. Keep it loaded: it's what `impeccable audit`, `impeccable polish`, etc. check against.

---

## Design Direction

Commit to a bold aesthetic direction:
- **Purpose**: What problem does this interface solve? Who uses it?
- **Tone**: Pick an extreme: brutally minimal, maximalist chaos, retro-futuristic, organic/natural, luxury/refined, playful/toy-like, editorial/magazine, brutalist/raw, art deco/geometric, soft/pastel, industrial/utilitarian, etc. There are so many flavors to choose from. Use these for inspiration but design one that is true to the aesthetic direction.
- **Constraints**: Technical requirements (framework, performance, accessibility).
- **Differentiation**: What makes this unforgettable? What's the one thing someone will remember?

Choose a clear conceptual direction and execute it with precision. Bold maximalism and refined minimalism both work; the key is intentionality rather than intensity.

Then implement working code that is:
- Production-grade and functional
- Visually striking and memorable
- Cohesive with a clear aesthetic point-of-view
- Meticulously refined in every detail

## Frontend Aesthetics Guidelines

### Typography
→ *Consult [typography reference](references/typography.md) for OpenType features, web font loading, and the deeper material on scales.*

Choose fonts that are beautiful, unique, and interesting. Pair a distinctive display font with a refined body font.

<typography_principles>
Always apply these — do not consult a reference, just do them:

- Use a modular type scale with fluid sizing (clamp) for headings on marketing/content pages. Use fixed `rem` scales for app UIs and dashboards (no major design system uses fluid type in product UI).
- Use fewer sizes with more contrast. A 5-step scale with at least a 1.25 ratio between steps creates clearer hierarchy than 8 sizes that are 1.1× apart.
- Line-height scales inversely with line length. Narrow columns want tighter leading, wide columns want more. For light text on dark backgrounds, add 0.05-0.1 to your normal line-height — light type reads as lighter weight and needs more breathing room.
- Cap line length at ~65-75ch. Body text wider than that is fatiguing.
</typography_principles>

<font_selection_procedure>
Run this procedure before typing any font name.

The model's natural failure mode is "I was told not to use Inter, so I will pick my next favorite font, which becomes the new monoculture." Avoid this by performing the following procedure on every project, in order:

Step 1. Read the brief once. Write down 3 concrete words for the brand voice (e.g., "warm and mechanical and opinionated", "calm and clinical and careful", "fast and dense and unimpressed", "handmade and a little weird"). Skip "modern" and "elegant" — those are dead categories.

Step 2. List the 3 fonts you would normally reach for given those words. Write them down. They are most likely from this list:

<reflex_fonts_to_reject>
Fraunces
Newsreader
Lora
Crimson
Crimson Pro
Crimson Text
Playfair Display
Cormorant
Cormorant Garamond
Syne
IBM Plex Mono
IBM Plex Sans
IBM Plex Serif
Space Mono
Space Grotesk
Inter
DM Sans
DM Serif Display
DM Serif Text
Outfit
Plus Jakarta Sans
Instrument Sans
Instrument Serif
</reflex_fonts_to_reject>

Reject every font that appears in the reflex_fonts_to_reject list. They are your training-data defaults and they create monoculture across projects.

Step 3. Browse a font catalog with the 3 brand words in mind. Sources: Google Fonts, Pangram Pangram, Future Fonts, Adobe Fonts, ABC Dinamo, Klim Type Foundry, Velvetyne. Look for something that fits the brand as a *physical object* — a museum exhibit caption, a hand-painted shop sign, a 1970s mainframe terminal manual, a fabric label on the inside of a coat, a children's book printed on cheap newsprint. Reject the first thing that "looks designy" — that's the trained reflex too. Keep looking.

Step 4. Cross-check the result. The right font for an "elegant" brief is not necessarily a serif. The right font for a "technical" brief is not necessarily a sans-serif. The right font for a "warm" brief is not Fraunces. If your final pick lines up with your reflex pattern, go back to Step 3.
</font_selection_procedure>

<typography_rules>
Use a modular type scale with fluid sizing (clamp) for marketing/content
headings. Use fixed rem scales for app UI and dashboard headings.
Vary font weights and sizes to create clear visual hierarchy.
Vary your font choices across projects. If you used a serif display font on the last project, look for a sans, monospace, or display face on this one.

Skip overused fonts like Inter, Roboto, Arial, Open Sans, and system defaults — and skip your second-favorite too: every font in the reflex_fonts_to_reject list above is banned. Look further.
Avoid monospace typography as lazy shorthand for "technical/developer" vibes.
Avoid large icons with rounded corners above every heading; they rarely add value and make sites look templated.
Avoid setting the whole page in one font family. Pair a distinctive display font with a refined body font.
Avoid a flat type hierarchy where sizes are too close together. Aim for at least a 1.25 ratio between steps.
Avoid long body passages in uppercase. Reserve all-caps for short labels and headings.
</typography_rules>

### Color & Theme
→ *Consult [color reference](references/color-and-contrast.md) for the deeper material on contrast, accessibility, and palette construction.*

Commit to a cohesive palette. Dominant colors with sharp accents outperform timid, evenly-distributed palettes.

<color_principles>
Always apply these — do not consult a reference, just do them:

- Use OKLCH, not HSL. OKLCH is perceptually uniform: equal steps in lightness *look* equal, which HSL does not deliver. As you move toward white or black, reduce chroma — high chroma at extreme lightness looks garish. A light blue at 85% lightness wants ~0.08 chroma, not the 0.15 of your base color.
- Tint your neutrals toward your brand hue. Even a chroma of 0.005-0.01 is perceptible and creates subconscious cohesion between brand color and UI surfaces. The hue you tint toward should come from THIS brand, not from a "warm = friendly" or "cool = tech" formula. Pick the brand's actual hue first, then tint everything toward it.
- The 60-30-10 rule is about visual *weight*, not pixel count. 60% neutral / surface, 30% secondary text and borders, 10% accent. Accents work because they're rare. Overuse kills their power.
</color_principles>

<theme_selection>
Derive the theme (light vs dark) from audience and viewing context rather than picking a default. Read the brief and ask: when is this product used, by whom, in what physical setting?

- A perp DEX consumed during fast trading sessions → dark
- A hospital portal consumed by anxious patients on phones late at night → light
- A children's reading app → light
- A vintage motorcycle forum where users sit in their garage at 9pm → dark
- An observability dashboard for SREs in a dark office → dark
- A wedding planning checklist for couples on a Sunday morning → light
- A music player app for headphone listening at night → dark
- A food magazine homepage browsed during a coffee break → light

Do not default everything to light "to play it safe." Do not default everything to dark "to look cool." Both defaults are the lazy reflex. The correct theme is the one the actual user wants in their actual context.
</theme_selection>

<color_rules>
Use modern CSS color functions (oklch, color-mix, light-dark) for perceptually uniform, maintainable palettes.
Tint your neutrals toward your brand hue. Even a subtle hint creates subconscious cohesion.

Avoid gray text on colored backgrounds; it looks washed out. Use a shade of the background color instead.
Avoid pure black (#000) and pure white (#fff). Always tint; pure black/white never appears in nature.
Avoid the AI color palette: cyan-on-dark, purple-to-blue gradients, neon accents on dark backgrounds.
Avoid gradient text for impact — see <absolute_bans> below for the strict definition. Solid colors only for text.
Avoid defaulting to dark mode with glowing accents. It looks "cool" without requiring actual design decisions.
Avoid defaulting to light mode "to be safe" either. The point is to choose, not to retreat to a safe option.
</color_rules>

### Layout & Space
→ *Consult [spatial reference](references/spatial-design.md) for the deeper material on grids, container queries, and optical adjustments.*

Create visual rhythm through varied spacing, not the same padding everywhere. Embrace asymmetry and unexpected compositions. Break the grid intentionally for emphasis.

<spatial_principles>
Always apply these — do not consult a reference, just do them:

- Use a 4pt spacing scale with semantic token names (`--space-sm`, `--space-md`), not pixel-named (`--spacing-8`). Scale: 4, 8, 12, 16, 24, 32, 48, 64, 96. 8pt is too coarse — you'll often want 12px between two values.
- Use `gap` instead of margins for sibling spacing. It eliminates margin collapse and the cleanup hacks that come with it.
- Vary spacing for hierarchy. A heading with extra space above it reads as more important — make use of that. Don't apply the same padding everywhere.
- Self-adjusting grid pattern: `grid-template-columns: repeat(auto-fit, minmax(280px, 1fr))` is the breakpoint-free responsive grid for card-style content.
- Container queries are for components, viewport queries are for page layout. A card in a sidebar should adapt to the sidebar's width, not the viewport's.
</spatial_principles>

<spatial_rules>
Create visual rhythm through varied spacing: tight groupings, generous separations.
Use fluid spacing with clamp() that breathes on larger screens.
Use asymmetry and unexpected compositions; break the grid intentionally for emphasis.

Avoid wrapping everything in cards. Not everything needs a container.
Avoid nesting cards inside cards. Visual noise; flatten the hierarchy.
Avoid identical card grids (same-sized cards with icon + heading + text, repeated endlessly).
Avoid the hero metric layout template (big number, small label, supporting stats, gradient accent).
Avoid centering everything. Left-aligned text with asymmetric layouts feels more designed.
Avoid the same spacing everywhere. Without rhythm, layouts feel monotonous.
Keep body text from wrapping beyond ~80 characters per line. Add a max-width like 65–75ch so the eye can track easily.
</spatial_rules>

### Visual Details

<absolute_bans>
These two CSS patterns are the most recognizable AI design tells, and no context makes them acceptable. Match-and-refuse: if you find yourself about to write either, stop and rewrite the element with a different structure entirely.

**Ban 1 — never use side-stripe borders** on cards, list items, callouts, or alerts.
  - Pattern: `border-left:` or `border-right:` with width greater than 1px — hard-coded colors and CSS variables alike (`border-left: 3px solid red`, `border-left: 4px solid #ff0000`, `border-left: 4px solid var(--color-warning)`, `border-left: 5px solid oklch(...)`, etc.).
  - Why: this is the single most overused "design touch" in admin, dashboard, and medical UIs. It never looks intentional regardless of color, radius, opacity, or whether the variable name is "primary" or "warning" or "accent."
  - Rewrite: use a different element structure entirely — full borders, background tints, leading numbers/icons, or no visual indicator at all. Swapping to an inset box-shadow is the same tell.

**Ban 2 — never use gradient text.**
  - Pattern: `background-clip: text` (or `-webkit-background-clip: text`) combined with a gradient background — any combination that makes text fill come from a `linear-gradient`, `radial-gradient`, or `conic-gradient`.
  - Why: gradient text is decorative rather than meaningful and is one of the top three AI design tells.
  - Rewrite: use a single solid color for text. If you want emphasis, use weight or size, not gradient fill.
</absolute_bans>

Use intentional, purposeful decorative elements that reinforce brand.
Avoid colored accent stripes (border-left or border-right greater than 1px) on cards, list items, callouts, or alerts. See <absolute_bans> above for the strict CSS pattern.
Avoid glassmorphism everywhere (blur effects, glass cards, glow borders used decoratively rather than purposefully).
Avoid sparklines as decoration. Tiny charts that look sophisticated but convey nothing meaningful.
Avoid rounded rectangles with generic drop shadows. Safe, forgettable, could be any AI output.
Avoid modals unless there's truly no better alternative. Modals are lazy.

### Motion

→ *Consult [motion reference](references/motion-design.md) for timing, easing, and reduced motion.*

Focus on high-impact moments: one well-orchestrated page load with staggered reveals creates more delight than scattered micro-interactions.

- Use motion to convey state changes: entrances, exits, feedback.
- Use exponential easing (ease-out-quart/quint/expo) for natural deceleration.
- For height animations, use grid-template-rows transitions instead of animating height directly.
- Animate transform and opacity only; layout properties (width, height, padding, margin) trigger reflow and jank.
- Avoid bounce and elastic easing. They feel dated and tacky; real objects decelerate smoothly.

### Interaction

→ *Consult [interaction reference](references/interaction-design.md) for forms, focus, and loading patterns.*

Make interactions feel fast. Use optimistic UI: update immediately, sync later.

- Use progressive disclosure. Start simple, reveal sophistication through interaction (basic options first, advanced behind expandable sections; hover states that reveal secondary actions).
- Design empty states that teach the interface, not just say "nothing here".
- Make every interactive surface feel intentional and responsive.
- Avoid repeating the same information (redundant headers, intros that restate the heading).
- Avoid making every button primary. Use ghost buttons, text links, secondary styles; hierarchy matters.

### Responsive

→ *Consult [responsive reference](references/responsive-design.md) for mobile-first, fluid design, and container queries.*

- Use container queries (@container) for component-level responsiveness.
- Adapt the interface for different contexts, not just shrink it.
- Keep critical functionality on mobile. Adapt the interface, don't amputate it.

### UX Writing

→ *Consult [ux-writing reference](references/ux-writing.md) for labels, errors, and empty states.*

- Make every word earn its place.
- Avoid repeating information users can already see.

---

## The AI Slop Test

The quality check: if you showed this interface to someone and said "AI made this," would they believe you immediately? If yes, that's the problem.

A distinctive interface should make someone ask "how was this made?" not "which AI made this?"

Review the avoid-rules above. They are the fingerprints of AI-generated work from 2024-2025.

---

## Implementation Principles

Match implementation complexity to the aesthetic vision. Maximalist designs need elaborate code with extensive animations and effects. Minimalist or refined designs need restraint, precision, and careful attention to spacing, typography, and subtle details.

Interpret creatively and make unexpected choices that feel genuinely designed for the context. No design should be the same. Vary between light and dark themes, different fonts, different aesthetics — converging on common choices across generations is the monoculture this skill exists to break.

Remember: you are capable of extraordinary creative work. Don't hold back. Show what can truly be created when thinking outside the box and committing fully to a distinctive vision.

---

## Teach Mode

If this skill is invoked with the argument `teach` (e.g., the user asks to `impeccable teach`), skip all design work above and instead run the teach flow below. This is a one-time setup that gathers design context for the project.

### Step 1: Explore the Codebase

Before asking questions, thoroughly scan the project to discover what you can:

- **README and docs**: Project purpose, target audience, any stated goals
- **Package.json / config files**: Tech stack, dependencies, existing design libraries
- **Existing components**: Current design patterns, spacing, typography in use
- **Brand assets**: Logos, favicons, color values already defined
- **Design tokens / CSS variables**: Existing color palettes, font stacks, spacing scales
- **Any style guides or brand documentation**

Note what you've learned and what remains unclear.

### Step 2: Ask UX-Focused Questions

Ask the user. Focus only on what you couldn't infer from the codebase:

#### Users & Purpose
- Who uses this? What's their context when using it?
- What job are they trying to get done?
- What emotions should the interface evoke? (confidence, delight, calm, urgency, etc.)

#### Brand & Personality
- How would you describe the brand personality in 3 words?
- Any reference sites or apps that capture the right feel? What specifically about them?
- What should this explicitly not look like? Any anti-references?

#### Aesthetic Preferences
- Any strong preferences for visual direction? (minimal, bold, elegant, playful, technical, organic, etc.)
- Light mode, dark mode, or both?
- Any colors that must be used or avoided?

#### Accessibility & Inclusion
- Specific accessibility requirements? (WCAG level, known user needs)
- Considerations for reduced motion, color blindness, or other accommodations?

Skip questions where the answer is already clear from the codebase exploration.

### Step 3: Write Design Context

Synthesize your findings and the user's answers into a `## Design Context` section:

```markdown
## Design Context

### Users
[Who they are, their context, the job to be done]

### Brand Personality
[Voice, tone, 3-word personality, emotional goals]

### Aesthetic Direction
[Visual tone, references, anti-references, theme]

### Design Principles
[3-5 principles derived from the conversation that should guide all design decisions]
```

Write this section to `.impeccable.md` in the project root. If the file already exists, update the Design Context section in place.

Then ask the user whether they'd also like the Design Context appended to the project instructions file (e.g. `CLAUDE.md` or `AGENTS.md`, whichever the project uses). If yes, append or update the section there as well.

Confirm completion and summarize the key design principles that will now guide all future work.
