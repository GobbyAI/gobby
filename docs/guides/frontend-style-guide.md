# Frontend Style Guide

Design standards, tokens, and implementation patterns for the Gobby web UI.

## Overview

Gobby's frontend is a single-page React application in `web/`. It is built with:

- **React 18** and **TypeScript 5** for components and type safety.
- **Vite 6** for the dev server and production build.
- **Tailwind CSS 4** with `@tailwindcss/vite` for utility styling.
- **Radix UI** primitives for dialog, select, slot, and tooltip behavior.
- **class-variance-authority (CVA)** for component variants.
- **clsx** and **tailwind-merge** through the shared `cn()` utility.

The product UI is dense, dark by default, and token-driven. Styling should feel
industrial, efficient, and calm: solid colors, sharp type hierarchy, subtle
motion, and no decorative gradients.

The authoritative design contract is `.impeccable.md`; this guide is the
implementation source of truth for how that contract is built. Its rules are
enforced mechanically by the style-debt ratchet
(`web/src/__tests__/styleRatchet.test.ts` — see
[Style Debt Ratchet](#style-debt-ratchet)). The deployed product UI
tokens live in `web/src/styles/tokens.css` (`web/src/styles/index.css` only
aggregates imports); Tailwind exposes the common tokens through the `@theme`
blocks in `web/src/styles/tailwind-theme.css`, while `web/tailwind.config.ts`
only sets content scanning.

## Design Tokens

Theme values live as CSS custom properties in `web/src/styles/tokens.css`.
`:root` is the dark theme. `[data-theme="light"]` overrides values for light
theme. `useSettings.ts` writes the selected or system-resolved theme to
`<html data-theme="...">`.

### Core Palette

Use token names instead of raw hex values. The palette uses OKLCH values and a
brand-tinted neutral ramp.

| Token | Dark | Light | Usage |
|-------|------|-------|-------|
| `--bg-primary` | `oklch(15% 0.005 125)` | `oklch(96.5% 0.006 125)` | Page background |
| `--bg-secondary` | `oklch(19% 0.006 125)` | `oklch(94% 0.008 125)` | Cards, sidebars, panels |
| `--bg-tertiary` | `oklch(23% 0.007 125)` | `oklch(90% 0.008 125)` | Hover states, muted areas |
| `--text-primary` | `oklch(92% 0.004 125)` | `oklch(20% 0.005 125)` | Body text, headings |
| `--text-secondary` | `oklch(68% 0.005 125)` | `oklch(40% 0.005 125)` | Secondary labels |
| `--text-muted` | `oklch(62% 0.005 125)` | `oklch(48% 0.005 125)` | Timestamps, placeholders, hints |
| `--border` | `oklch(28% 0.008 125)` | `oklch(80% 0.008 125)` | Borders and dividers |

### Accent Tokens

Gobby's brand accent is chartreuse at hue 125. It is deliberately separated
from destructive/error hues for deutan accessibility.

| Token | Dark | Light | Usage |
|-------|------|-------|-------|
| `--accent` | `oklch(82% 0.20 125)` | `oklch(50% 0.18 125)` | Links, active tabs, primary actions |
| `--accent-hover` | `oklch(75% 0.22 125)` | `oklch(42% 0.18 125)` | Accent hover state |
| `--accent-foreground` | `oklch(15% 0 0)` | `oklch(99% 0 0)` | Text on accent backgrounds |
| `--accent-soft` | `oklch(82% 0.20 125 / 0.18)` | `oklch(50% 0.18 125 / 0.18)` | Soft accent surfaces |
| `--accent-tint` | `oklch(82% 0.20 125 / 0.10)` | `oklch(50% 0.18 125 / 0.10)` | Active nav and subtle emphasis |

### Semantic Colors

State colors must not rely on hue alone. Pair color with text, icon, position,
or shape.

The `info`, `warning`, `error`, and `success` lanes share one foreground-first
sibling contract:

| Token pattern | Usage |
|---------------|-------|
| `--color-<state>` | Readable state text, icons, dots, and borders |
| `--color-<state>-foreground` | Same readable value as the bare foreground token |
| `--color-<state>-bg` | Solid state surface |
| `--color-<state>-soft` | Soft state surface |
| `--color-<state>-tint` | Subtle state emphasis |
| `--text-on-<state>` | Text placed on the solid `-bg` state surface |

Destructive actions retain the dedicated `--color-destructive` surface and
`--color-destructive-foreground` text pair. Review aliases the info lane;
inactive uses the neutral hue-125 lane. Exact dark and light values live only
in `web/src/styles/tokens.css`.

Specialized palettes for language icons, git status, pipeline steps, execution
status, session sources, providers, integration channels, task categories, and
agent isolation live in `web/src/styles/tokens.css`. The TypeScript source of
truth for the mirrored color pairs is in:

- `web/src/lib/languageColors.ts`
- `web/src/lib/pipelineColors.ts`
- `web/src/components/shared/sourceTheme.ts`
- `web/src/components/integrations/channelMetadata.ts`
- `web/src/components/agents/categoryColors.ts`
- `web/src/components/workflows/isolationColors.ts`

Do not introduce a dedicated agent hue. Agents use existing state/source tokens.

### Surface And Shadow Tokens

| Token | Usage |
|-------|-------|
| `--surface-scrim` | Modal and sidebar overlays |
| `--surface-tint-subtle` | Subtle layered backgrounds |
| `--shadow-sm` through `--shadow-xl` | Elevation shadows |
| `--shadow-popover-up` | Popovers that open upward |
| `--shadow-panel-left` | Right-side panels casting leftward shadow |

### Message And Code Tokens

| Token | Usage |
|-------|-------|
| `--user-bg` | User message background |
| `--assistant-bg` | Assistant message background |
| `--system-bg` | System/error message background |
| `--code-bg` | Inline/chrome code background |
| `--code-bg-block` | Code block background |
| `--code-gutter-border` | Code gutter divider |
| `--code-gutter-text` | Code gutter line numbers |
| `--code-active-line-bg` | Active line highlight |

### Layout Tokens

| Token | Value | Notes |
|-------|-------|-------|
| `--sidebar-width` | `260px` | Sidebar default width; narrow widths compact through container queries |
| `--font-size-base` | `16px` | User-adjustable through Settings |
| `--gobby-logo-image` | `url("/logo.png")` or `url("/logo-light.png")` | Theme-aware logo asset |

## Typography

### Font Stacks

| Purpose | Variable | Stack |
|---------|----------|-------|
| UI text | `--font-sans` | `"Geist Variable", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` |
| Code | `--font-mono` | `"JetBrains Mono Variable", "SF Mono", "Fira Code", monospace` |

The app imports Geist through `@fontsource-variable/geist`. Do not add a display
font to the product UI.

### Type Scale

All text tokens scale from `--font-size-base`, so the Settings slider resizes
the UI consistently.

| Token | Formula | 16px base | Tailwind key |
|-------|---------|-----------|--------------|
| `--text-2xs` | `* 0.625` | 10px | `text-2xs` |
| `--text-xs` | `* 0.6875` | 11px | `text-xs` |
| `--text-sm` | `* 0.75` | 12px | `text-sm` |
| `--text-md` | `* 0.8125` | 13px | `text-md` |
| `--text-base` | `* 0.875` | 14px | `text-base` |
| `--text-lg` | `* 1` | 16px | `text-lg` |
| `--text-xl` | `* 1.125` | 18px | `text-xl` |
| `--text-2xl` | `* 1.25` | 20px | `text-2xl` |
| `--text-3xl` | `* 1.5` | 24px | `text-3xl` |
| `--text-4xl` | `* 2` | 32px | `text-4xl` |

Use Tailwind font-size keys when styling JSX. Use CSS variables in CSS files.

### Font Weights

| Token | Value | Usage |
|-------|-------|-------|
| `--font-weight-normal` | 400 | Body text |
| `--font-weight-medium` | 500 | Active states and interactive labels |
| `--font-weight-semibold` | 600 | Headings, badges, buttons |
| `--font-weight-bold` | 700 | Strong emphasis |

Tailwind maps these as `font-normal`, `font-medium`, `font-semibold`, and
`font-bold`.

## Tailwind Usage

Tailwind scans `./src/**/*.{ts,tsx}` and utilities win by cascade-layer order
alone — the `important: true` era is over (plan 6.1). The cascade model:

- Utilities live in `@layer utilities`; global resets (`base.css`,
  `accessibility.css`) live in `@layer base`, which utilities out-rank.
- The surviving hook sheets (`app-shell.css`, `segmented-control.css`,
  `dropdown-caret.css`, `settings-overlay.css`) are un-layered, so their
  declarations out-cascade utilities regardless of specificity. A hook rule
  may therefore only style properties that no utility on the same element
  sets, in any state or variant. To let a caller vary a hook-styled property,
  expose a custom property (see `--segmented-option-px`) instead of expecting
  a utility to win.

Use semantic Tailwind colors where mappings exist.

| Tailwind class | CSS variable |
|----------------|--------------|
| `bg-background` | `--bg-primary` |
| `text-foreground` | `--text-primary` |
| `bg-muted` | `--bg-tertiary` |
| `text-muted-foreground` | `--text-secondary` |
| `bg-accent` | `--accent` |
| `text-accent-foreground` | `--accent-foreground` |
| `hover:bg-accent-hover` | `--accent-hover` |
| `border-border` | `--border` |
| `bg-destructive` | `--color-destructive` |
| `text-destructive-foreground` | `--color-destructive-foreground` |
| `text-warning` | `--color-warning` |
| `bg-warning-bg` | `--color-warning-bg` |
| `text-warning-foreground` | `--color-warning-foreground` |
| `text-success` | `--color-success` |
| `bg-success-bg` | `--color-success-bg` |
| `text-success-foreground` | `--color-success-foreground` |
| `text-error` | `--color-error` |
| `bg-error-bg` | `--color-error-bg` |
| `bg-error-soft` | `--color-error-soft` |
| `text-info` | `--color-info` |
| `bg-info-bg` | `--color-info-bg` |
| `bg-info-soft` | `--color-info-soft` |
| `text-review` | `--color-review` |
| `bg-review-soft` | `--color-review-soft` |

For tokens without Tailwind aliases, use arbitrary value classes:

```tsx
<span className="text-[var(--source-codex)]" />
<div className="bg-[var(--surface-scrim)]" />
```

Use `resolveCssVar()` from `web/src/lib/utils.ts` when canvas or Three.js code
needs a concrete RGB/RGBA value. It resolves OKLCH custom properties through the
browser and caches by theme.

## Layout And Spacing

Use Tailwind's default spacing scale for component layout. The base unit is
4px: `1` equals `0.25rem`.

| Tailwind | CSS | Usage |
|----------|-----|-------|
| `gap-1.5` | `0.375rem` | Tight groups |
| `gap-2` | `0.5rem` | Standard element spacing |
| `gap-3` | `0.75rem` | Dense section spacing |
| `gap-4` | `1rem` | Section padding |
| `gap-6` | `1.5rem` | Page-level spacing |

Prefer predictable density over large decorative whitespace. Interactive
targets stay touch-friendly automatically: non-`dense` `Button`s promote to the
44px floor on coarse pointers (see the Button section).

### Border Radius

| Value | Usage |
|-------|-------|
| `0.25rem` | Inline code and compact controls |
| `0.375rem` | Buttons and inputs |
| `0.5rem` | Cards, messages, panels |
| `9999px` | Pills and status dots |
| `50%` | Circular indicators |

## Components

### Shared UI Primitives

All shared primitives live in `web/src/components/ui/`. The ratchet's raw-element
check exists so interactive elements are built from these primitives — only files
under `src/components/ui/` may render raw `<button>`, `<input>`, `<select>`, or
`<textarea>` freely.

| Component | Purpose |
|-----------|---------|
| `Button.tsx` | CVA button wrapper with Radix `Slot` support through `asChild` |
| `buttonVariants.ts` | The `buttonVariants` CVA definition (variant/size/dense) |
| `Badge.tsx` | Status badge variants |
| `Dialog.tsx` | Radix dialog overlay, content, title, and description wrappers |
| `dialogPrimitives.ts` | Re-exported Radix dialog root/trigger/close primitives |
| `ConfirmDialog.tsx` | Confirmation dialog built from `Dialog` and `Button` |
| `DropdownCaret.tsx` | Shared caret affordance for dropdown triggers |
| `Input.tsx` | Standard text input |
| `Textarea.tsx` | Standard textarea |
| `Select.tsx` | Radix select trigger, content, and item wrappers |
| `selectPrimitives.ts` | Re-exported Radix select root/group/value/label primitives |
| `SegmentedControl.tsx` | Segmented control (mutually exclusive option row) |
| `ScrollArea.tsx` | Scroll area wrapper |
| `Switch.tsx` | Toggle switch |
| `Tooltip.tsx` | Radix tooltip content wrapper |
| `tooltipPrimitives.ts` | Re-exported Radix tooltip provider/root/trigger/portal primitives |

#### Button

The `.btn` CSS class system is retired; `Button` is the only sanctioned button
surface. Choose the variant by role, not by look:

```tsx
import { Button } from '../ui/Button' // path relative to the caller

<Button variant="accent" size="sm">Refresh</Button>   // canonical action style
<Button variant="primary">Send</Button>               // the surface's single dominant CTA
<Button>Cancel</Button>                               // secondary (default)
<Button variant="ghost" size="icon"><MyIcon /></Button>
<Button variant="destructive" size="sm">Delete</Button>
```

| Variant | Role | Appearance |
|---------|------|------------|
| `secondary` (default) | Ordinary actions | Bordered, transparent, muted text |
| `accent` | The canonical style for meaningful actions (New Chat, Hide Panel) | Tinted accent surface and border |
| `primary` | The single dominant CTA on a surface (chat Send, Approve) | Solid accent background |
| `ghost` | Tertiary actions in dense rows | Borderless, transparent, muted hover |
| `destructive` | Quiet destructive actions | Transparent with error text; error-soft hover |
| `outline` | Neutral bordered alternative | Transparent with border, foreground text |

| Size | Classes |
|------|---------|
| `sm` | `min-h-7 px-2.5 text-xs` |
| `md` (default) | `min-h-8 px-3.5 text-sm` |
| `lg` | `min-h-10 px-4.5 text-base` |
| `icon` | `min-h-8 w-8 p-0 text-sm` |

**Coarse pointers and `dense`.** By default every `Button` promotes itself to
the 44px touch floor on coarse pointers (`pointer-coarse:min-h-11
pointer-coarse:min-w-11`). Pass `dense` only for desktop-only chrome that must
keep its compact row height on touch — the app-header cluster and the
status-bar/command-bar controls pinned to `--status-bar-control-height`.
Content-area buttons stay non-dense.

Surface-specific sizing hooks (`app-settings-cog`, `command-bar-btn`, …) stay as
plain `className` hook classes; their rules live in the owning stylesheet
(`app-shell.css`, `layout.css`).

#### Badge

```tsx
import { Badge } from './ui/Badge'

<Badge variant="success">Connected</Badge>
<Badge variant="error">Failed</Badge>
<Badge variant="warning">Pending</Badge>
<Badge variant="info">Running</Badge>
<Badge>Default</Badge>
```

Badge variants are `default`, `success`, `warning`, `error`, and `info`.

#### Dialog

```tsx
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from './ui/Dialog'

<Dialog>
  <DialogTrigger asChild><Button>Open</Button></DialogTrigger>
  <DialogContent>
    <DialogTitle>Confirm action</DialogTitle>
    <DialogDescription>Review the action before continuing.</DialogDescription>
  </DialogContent>
</Dialog>
```

Use `ConfirmDialog` for common confirm/cancel flows.

### CVA Pattern

Use CVA for reusable components that need variants.

```tsx
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../../../lib/utils'

const variants = cva('base classes', {
  variants: {
    variant: {
      default: 'bg-muted text-foreground',
      active: 'bg-accent text-accent-foreground',
    },
  },
  defaultVariants: {
    variant: 'default',
  },
})

interface Props
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof variants> {}

export function MyComponent({ className, variant, ...props }: Props) {
  return <div className={cn(variants({ variant, className }))} {...props} />
}
```

### `cn()` Utility

Use `cn()` for conditional classes and caller-provided `className` merging.

```tsx
import { cn } from '../../../lib/utils'

<div className={cn('p-4 bg-background', isActive && 'border border-accent', className)} />
```

## Styling Approach

### Tailwind First

Use Tailwind utilities for layout, spacing, text, borders, and simple states.

```tsx
<div className="flex items-center gap-2 rounded-lg border border-border bg-background p-4">
  <span className="text-sm text-muted-foreground">Label</span>
</div>
```

### CSS Variables For Theme Values

Use semantic Tailwind mappings or CSS variables.

```tsx
<div className="border border-border bg-background text-foreground" />
<div className="bg-muted text-muted-foreground" />
<div className="text-accent" />
<div className="bg-[var(--color-info-soft)] text-[var(--color-info)]" />
```

Avoid raw hex and one-off color literals in components.

### Legacy CSS Files

**New stylesheets are banned.** The ratchet records the exact set of existing
CSS files and fails on any addition. New styling goes in Tailwind utilities at
the call site, `cva` definitions for variants, or a `components/ui` primitive.

The existing sheets (feature-scoped modules for integrations, agents,
workflows, and chat tabs; global sheets under `web/src/styles/`, including
the allowlisted `src/styles/dropdown-caret.css`) are tracked exceptions. They
remain in the exact allowlist and do not permit new stylesheets.
This legacy debt remains under a total line ceiling that only shrinks. When
editing one:

- Keep to its established BEM-style naming (`.my-feature__item--active`).
- Prefer deleting rules by migrating the styled markup onto primitives and
  utilities over adding rules.
- When a sheet is emptied, delete it and remove its `CSS_FILE_ALLOWLIST` entry.

### Light Mode

Prefer variables that already switch under `[data-theme="light"]`. Add manual
light overrides only when a component introduces hardcoded theme-specific
styling.

```css
.my-status {
  background: var(--color-success-soft);
  color: var(--color-success-foreground);
}
```

For tinted backgrounds, use alpha OKLCH tokens or `color-mix()` with existing
tokens.

```css
background: color-mix(in srgb, var(--accent) 10%, transparent);
```

## Style Debt Ratchet

`web/src/__tests__/styleRatchet.test.ts` scans `web/src/` (skipping test
directories, `.test.`/`.spec.` files, and `.d.ts`) and compares the result
against the recorded debt in `web/src/__tests__/styleRatchet.allowlist.ts`.

**Attrition contract: allowlist entries may only be deleted or decreased.**
Never add an entry, never increase a count, never raise the line ceiling. Every
check fails in both directions — above the ceiling means new debt was
introduced; below it means the allowlist is stale and must be tightened to the
new, lower count. A passing run therefore proves the recorded debt is exact.

The six checks:

| Check | Rule | Current state |
|-------|------|---------------|
| `.btn` class tokens | `btn`/`btn-*` string literals in ts/tsx (comment-stripped; suffixed hook classes like `command-bar-btn` don't match) | Pure ban — `BTN_CLASS_ALLOWLIST` is empty; the `.btn` system is retired |
| Raw interactive elements | `<button>`, `<input>`, `<select>`, `<textarea>` in tsx outside `src/components/ui/` | Per-file exact counts; migrate onto primitives to shrink them |
| `*_CLS` constants | `const FOO_CLS = ...` style-string constants | Per-file exact counts; style at the call site instead |
| Stylesheet set | Every `.css` file under `src/` must appear in `CSS_FILE_ALLOWLIST` | New CSS files are banned; deleted sheets must drop their entry |
| `!important` | Per-file exact counts across ts/tsx/css | Never add one; fix specificity at the source |
| Total CSS lines | Sum of all stylesheet lines vs `CSS_TOTAL_LINE_CEILING` | Ceiling only lowers; when total drops more than `CSS_LINE_TIGHTEN_SLACK` below it, the test demands lowering the ceiling |

To pay down debt: migrate a call site (raw element → primitive, `*_CLS` →
inline utilities, CSS rule → utility classes), run the ratchet, and tighten the
allowlist entries it reports as stale. The failure messages name the exact file
and remedy.

## Icons

Gobby uses inline SVG React components. Do not add an icon library.

Shared icons live in:

- `web/src/components/icons/AppIcons.tsx` for app navigation icons.
- `web/src/components/shared/Icons.tsx` for reusable shared-surface icons.
- Local component files for one-off icons.

Follow the established inline SVG pattern:

```tsx
export function SearchIcon({ size = 14 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  )
}
```

Use `currentColor` so icons inherit text color. Default sizes are usually 12px,
14px, 18px, or 20px depending on context.

## Animation And Transitions

Motion is functional and sparse. Use it for state changes, feedback, and panel
entrances.

| Duration | Easing | Usage |
|----------|--------|-------|
| `0.1s` | `ease` | Quick hover effects |
| `0.15s` | `ease` | Standard color and border transitions |
| `0.2s` | `ease-out` | Panel/modal entrance |
| `0.3s` | `ease` | Width or larger layout shifts |

Prefer property-specific transitions.

```css
.my-element {
  transition:
    background-color 0.15s,
    color 0.15s,
    border-color 0.15s;
}
```

Current shared keyframes include `fade-in`, `pulse-glow`, and
`reasoning-pulse` in `web/src/styles/base.css`; chat and reporting CSS define
additional feature-local keyframes.

## Dark And Light Mode

Theme behavior is implemented in `web/src/hooks/useSettings.ts`:

1. Settings initialize from `localStorage`.
2. `/api/config/ui-settings` is fetched on mount and wins over local storage.
3. `--font-size-base` is written to `document.documentElement.style`.
4. Theme is applied through `document.documentElement.setAttribute('data-theme', resolvedTheme)`.
5. `theme: "system"` follows `prefers-color-scheme` and updates on media-query changes.
6. Settings persist back to `localStorage` and `/api/config/ui-settings`.

Theme-aware styles should usually use CSS variables. Theme switching then
repaints without React-specific theme subscriptions.

## State Management Patterns

### Hook-First Architecture

State is managed with custom hooks in `web/src/hooks/`. The app does not use a
global state manager.

```tsx
const { settings, updateTheme, updateFontSize } = useSettings()
const { tasks, createTask } = useTasks(projectId)
const { servers, toolsByServer, fetchToolSchema } = useMcp()
```

### Persistence

Settings use a write-through pattern:

1. Write to `localStorage` for immediate local persistence.
2. Write to `/api/config/ui-settings` best-effort.
3. On mount, fetch from the API and merge over local storage.

Feature state should follow the same pattern only when local responsiveness and
daemon persistence are both required.

### Navigation

Top-level navigation is tab-based. `App.tsx` stores the active tab in local
state, initializes it from `window.location.hash`, and writes the hash when the
tab changes. Valid tabs and sidebar nav items live in
`web/src/components/app/appNavigation.tsx`.

Pages are lazy-loaded through `web/src/components/app/AppPages.tsx`.

## File Organization

```
web/src/
├── components/
│   ├── activity/           # Activity panel tabs (sessions, filters, modals)
│   ├── agents/             # Agent portfolio and agent run UI
│   ├── app/                # App shell helpers, navigation, lazy pages
│   ├── auth/               # Login and auth surfaces
│   ├── chat/               # Chat page, input, messages, chat-specific styles
│   ├── code/               # Code graph/code page surfaces
│   ├── code-graph/         # Code knowledge-graph visualization
│   ├── command-browser/    # Slash command and tool/skill browser modals
│   ├── cron/               # Cron run components
│   ├── dashboard/          # Dashboard cards and charts
│   ├── icons/              # App navigation icons
│   ├── integrations/       # Integration channel UI
│   ├── mcp/                # MCP server/tool UI
│   ├── memory/             # Memory browser UI
│   ├── projects/           # Project list/detail/settings UI
│   ├── rules/              # Rule management UI
│   ├── shared/             # Shared components and shared icons
│   ├── skills/             # Skill browser/editor UI
│   ├── source-control/     # GitHub/source-control UI
│   ├── tasks/              # Task management UI
│   ├── traces/             # Trace UI
│   ├── ui/                 # Standalone primitives (SegmentedControl, Switch)
│   └── workflows/          # Workflow, agent, pipeline, and reports UI
├── contexts/               # React contexts used by cross-page surfaces
├── hooks/                  # Custom React hooks
├── lib/                    # API clients, color helpers, normalization, utils
├── styles/                 # Global and cross-feature CSS
├── types/                  # TypeScript type definitions
├── App.tsx                 # Root app shell
└── main.tsx                # React entry point
```

### Naming Conventions

| Type | Convention | Example |
|------|------------|---------|
| Component files | PascalCase | `AgentPortfolioPage.tsx`, `SessionsTab.tsx` |
| Hook files | camelCase with `use` prefix | `useSettings.ts`, `useTasks.ts` |
| CSS classes | kebab-case or BEM | `.sidebar-overlay`, `.sc-badge--green` |
| TypeScript types | PascalCase | `ChatMessage`, `TaskCreateDefaults` |
| Functions | camelCase | `handleSubmit`, `fetchDetail` |

### Where To Put New Code

| Creating... | Location |
|-------------|----------|
| Shared UI primitive | `web/src/components/ui/` |
| App shell or navigation helper | `web/src/components/app/` |
| Feature-specific component | `web/src/components/<feature>/` |
| New page/tab | `web/src/components/<feature>/` plus `AppPages.tsx` and `appNavigation.tsx` |
| Custom hook | `web/src/hooks/use<Name>.ts` |
| Shared app icon | `web/src/components/icons/AppIcons.tsx` |
| Shared non-nav icon | `web/src/components/shared/Icons.tsx` |
| New CSS | Nowhere — new stylesheets are banned; use Tailwind utilities at the call site |
| Type definitions | `web/src/types/` |

## Z-Index Scale

Use the existing scale unless a component must sit above a known layer.

| Z-index | Layer | Examples |
|---------|-------|----------|
| `50` | Sidebars and source-control panels | Sidebar surfaces |
| `100` | Popovers and common modals | Skill/import modals |
| `250` | Radix dialog overlay/content | `DialogOverlay`, `DialogContent` |
| `999` | Sidebar overlay | `.sidebar-overlay` |
| `1000` | Sidebar and critical toasts | `.sidebar`, `.app-toast` |

## Anti-Patterns

Do not:

- Add UI libraries, routers, state managers, animation libraries, or icon packs without explicit approval.
- Use CSS-in-JS libraries such as styled-components or emotion.
- Use inline styles except for dynamic computed values or tightly scoped legacy interop.
- Use raw hex colors in new UI code.
- Use gradients, gradient text, neon glows, or purple-blue AI-dashboard styling.
- Add React Router; top-level navigation is hash-backed tab state.
- Create broad global providers when a custom hook and local state will do.
- Use `!important`; utilities own their properties by cascade-layer order — fix
  specificity at the source, and never style a utility-owned property from a
  hook sheet (see [Tailwind Usage](#tailwind-usage)).
- Use `btn`/`btn-*` classes — the `.btn` system is retired; use `<Button>` from `components/ui`.
- Render raw `<button>`, `<input>`, `<select>`, or `<textarea>` outside `components/ui`; use the primitives.
- Create new `.css` files or new `*_CLS` style-string constants (ratchet-enforced).
- Treat light mode as a secondary pass.
- Create new agent-specific colors.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| `bg-[#0a0a0a]` | Use `bg-background` or a CSS variable |
| `text-[#e5e5e5]` | Use `text-foreground` |
| Raw red for errors | Use destructive/error tokens; destructive hue is magenta-pink |
| Color-only status indicator | Add text, icon, position, or shape |
| Forgetting light mode | Use variables that switch under `[data-theme="light"]` |
| New modal implementation | Use `Dialog` or `ConfirmDialog` from `components/ui/` |
| `<button className="...">` in a feature component | Use `<Button>` from `components/ui/` |
| `style={{ color: 'red' }}` | Use semantic classes or `text-[var(--color-error)]` |
| New global context for page-local state | Write a hook or local component state |
| String-concatenated class names | Use `cn()` |
| New icon package | Add an inline SVG component |

_Last verified: 2026-07-27_
