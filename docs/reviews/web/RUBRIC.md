# Review Rubric — Web (web/src, TS/React)

> **Starting point, not a straitjacket.** Web findings are judged by *this* rubric,
> not the Python one. The severity table, evidence discipline, and output format
> from `../RUBRIC.md` still apply (Blocker/Important/Nit; cite exact `file:line` or
> drop; mark uncertainty; no invented findings; write to
> `docs/reviews/web/<area>.md` via `../TEMPLATE.md`). Everything below is the
> web-specific *what to look for*. Expand as the area warrants.

## Before you start

**Read `.impeccable.md`** (repo root) in full. It is the locked design system; the
constraints below are its load-bearing rules, not a replacement for it. The shipped
token source is `web/src/styles/index.css`.

## Design system (violations are at least Important)

**Color — the user is deutan colorblind; this is structural, not cosmetic:**
- Brand accent is hue 125 (chartreuse). State palette is locked: Info 250 (blue),
  Warning 75 (amber), Destructive 350 (magenta-pink, **not red**), Success 125
  (lightness-only). Flag any hard-coded color that bypasses the tokens.
- **State never relies on hue alone** — every state-bearing element needs an icon
  or position cue and must survive a grayscale screenshot. Flag color-only state.
- **Banned:** red-on-green / green-on-red of any kind; pure `#000` / `#fff` (use
  tinted neutrals); cyan-on-near-black, purple→blue gradients, neon glow; gradient
  text. Flag each occurrence.
- **No agent-specific accent.** The legacy `--color-agent` (hue 320) is retired —
  flag any remaining use.

**Typography:**
- `--font-sans` Geist Sans, `--font-mono` JetBrains Mono. Fixed rem ladder
  (`--text-*`). Hierarchy from weight + size + spacing, **not color**.
- Banned families (flag if introduced): Inter, DM Sans/Serif, Outfit, Plus Jakarta,
  Instrument, IBM Plex *, Space Mono/Grotesk, Fraunces, Newsreader, Lora, Crimson,
  Playfair, Cormorant, Syne. No fluid `clamp()` type in product UI.

**Accessibility — WCAG 2.2 AA is the floor:**
- Focus rings mandatory, brand accent, AA contrast on every surface.
- Keyboard parity: every interactive element reachable and operable without pointer.
- `prefers-reduced-motion` honored on animations >~150ms; no motion-only feedback.
- Touch targets 44×44 minimum.
- Equal polish in dark **and** light — check both.

## React / TypeScript correctness

- **Hooks:** dependency arrays correct (no stale closures, no missing deps, no
  lying-to-the-linter `// eslint-disable`); effect cleanup present; no setState on
  unmounted; data-fetch **race conditions** (out-of-order responses, missing abort).
- **TS strict:** no `any` / unsafe casts smuggling past the type system; runtime
  boundary validation on external data (API responses, params) — types are not
  validation; discriminated unions over boolean soup.
- **Rendering:** stable `key`s (not index where order changes); memoization that
  actually helps vs cargo-culted `useMemo`; no expensive work in render.
- **State/data:** context provider correctness, no prop-drilling that should be
  context (or vice versa), cache invalidation, optimistic-update rollback.

## Out of scope

Anything `eslint`/`prettier`/`tsc` already enforce. Pure taste. Bikeshedding token
names that already ship.
