# Design execution

### Layout & Space

→ *Consult `references/spatial-design.md` for deeper material on grids, container queries, and optical adjustments.*

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

→ *Consult `references/motion-design.md` for timing, easing, and reduced motion.*

Focus on high-impact moments: one well-orchestrated page load with staggered reveals creates more delight than scattered micro-interactions.

- Use motion to convey state changes: entrances, exits, feedback.
- Use exponential easing (ease-out-quart/quint/expo) for natural deceleration.
- For height animations, use grid-template-rows transitions instead of animating height directly.
- Animate transform and opacity only; layout properties (width, height, padding, margin) trigger reflow and jank.
- Avoid bounce and elastic easing. They feel dated and tacky; real objects decelerate smoothly.

### Interaction

→ *Consult `references/interaction-design.md` for forms, focus, and loading patterns.*

Make interactions feel fast. Use optimistic UI: update immediately, sync later.

- Use progressive disclosure. Start simple, reveal sophistication through interaction (basic options first, advanced behind expandable sections; hover states that reveal secondary actions).
- Design empty states that teach the interface, not just say "nothing here".
- Make every interactive surface feel intentional and responsive.
- Avoid repeating the same information (redundant headers, intros that restate the heading).
- Avoid making every button primary. Use ghost buttons, text links, secondary styles; hierarchy matters.

### Responsive

→ *Consult `references/responsive-design.md` for mobile-first, fluid design, and container queries.*

- Use container queries (@container) for component-level responsiveness.
- Adapt the interface for different contexts, not just shrink it.
- Keep critical functionality on mobile. Adapt the interface, don't amputate it.

### UX Writing

→ *Consult `references/ux-writing.md` for labels, errors, and empty states.*

- Make every word earn its place.
- Avoid repeating information users can already see.

## The AI Slop Test

The quality check: if you showed this interface to someone and said "AI made this," would they believe you immediately? If yes, that's the problem.

A distinctive interface should make someone ask "how was this made?" not "which AI made this?"

Review the avoid-rules above. They are the fingerprints of AI-generated work from 2024-2025.

## Implementation Principles

Match implementation complexity to the aesthetic vision. Maximalist designs need elaborate code with extensive animations and effects. Minimalist or refined designs need restraint, precision, and careful attention to spacing, typography, and subtle details.

Interpret creatively and make unexpected choices that feel genuinely designed for the context. No design should be the same. Vary between light and dark themes, different fonts, different aesthetics — converging on common choices across generations is the monoculture this skill exists to break.

Remember: you are capable of extraordinary creative work. Don't hold back. Show what can truly be created when thinking outside the box and committing fully to a distinctive vision.
