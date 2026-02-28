---
name: frontend-style
description: Design tokens, component patterns, and styling rules for the Gobby web UI. Use when building or modifying frontend components.
category: frontend
triggers: frontend, ui, component, styling, css, tailwind, design, theme, web
metadata:
  gobby:
    audience: all
    sources: [web]
    format_overrides:
      autonomous: full
---

# Frontend Style Guide

Rules and patterns for the Gobby web UI at `web/`.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Framework | React 18 + TypeScript 5 |
| Build | Vite 6 |
| CSS | Tailwind CSS 4 + CSS custom properties |
| Primitives | Radix UI (Dialog, Select, Tabs, Tooltip) |
| Variants | class-variance-authority (CVA) |
| Class merging | `cn()` from `web/src/lib/utils.ts` (clsx + tailwind-merge) |

## Design Tokens

All colors are CSS custom properties. Dark mode is the default (`:root`), light mode overrides via `[data-theme="light"]`.

### Core Palette

| Token | Dark | Light | Usage |
|-------|------|-------|-------|
| `--bg-primary` | `#0a0a0a` | `#ffffff` | Page background |
| `--bg-secondary` | `#141414` | `#f5f5f5` | Cards, panels |
| `--bg-tertiary` | `#1a1a1a` | `#e5e5e5` | Hover states, muted areas |
| `--text-primary` | `#e5e5e5` | `#171717` | Body text |
| `--text-secondary` | `#a3a3a3` | `#525252` | Secondary labels |
| `--text-muted` | `#737373` | `#a3a3a3` | Timestamps, placeholders |
| `--accent` | `#3b82f6` | `#2563eb` | Links, active states |
| `--accent-hover` | `#2563eb` | `#1d4ed8` | Accent hover |
| `--border` | `#262626` | `#d4d4d4` | All borders |

### Semantic Colors

| Token | Dark | Light | Usage |
|-------|------|-------|-------|
| `--color-destructive` | `#7f1d1d` | `#fecaca` | Destructive backgrounds |
| `--color-destructive-foreground` | `#f87171` | `#dc2626` | Destructive text |
| `--color-error` | `#f87171` | `#dc2626` | Error text |
| `--color-warning` | `#78350f` | `#fef3c7` | Warning backgrounds |
| `--color-warning-foreground` | `#f59e0b` | `#d97706` | Warning text |
| `--color-success` | `#14532d` | `#dcfce7` | Success backgrounds |
| `--color-success-foreground` | `#22c55e` | `#16a34a` | Success text |

### Message Backgrounds

| Token | Dark | Light |
|-------|------|-------|
| `--user-bg` | `#1e3a5f` | `#dbeafe` |
| `--assistant-bg` | `#1a1a1a` | `#f5f5f5` |
| `--system-bg` | `#2d1f1f` | `#fef2f2` |
| `--code-bg` | `#0d0d0d` | `#f0f0f0` |

### Layout

| Token | Value |
|-------|-------|
| `--sidebar-width` | `260px` |
| `--font-size-base` | `16px` (user-adjustable 12-48px) |

## Tailwind Config

Colors extend from CSS variables — use Tailwind semantic names, not raw hex:

```tsx
// Good
<div className="bg-background text-foreground border-border" />
<Badge variant="success" />
<Button variant="destructive" />

// Bad
<div className="bg-[#0a0a0a] text-[#e5e5e5]" />
<div style={{ color: '#f87171' }} />
```

Extended color map in `web/tailwind.config.ts`:

```text
background  → var(--bg-primary)
foreground  → var(--text-primary)
muted       → var(--bg-tertiary)        muted-foreground → var(--text-secondary)
accent      → var(--accent)             accent-foreground → var(--accent-foreground)
border      → var(--border)
destructive → var(--color-destructive)  destructive-foreground → var(--color-destructive-foreground)
warning     → var(--color-warning)      warning-foreground → var(--color-warning-foreground)
success     → var(--color-success)      success-foreground → var(--color-success-foreground)
```

`important: true` is set — Tailwind classes always win over base styles.

## Typography

**Font stacks:**
- UI text: `--font-sans` = `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`
- Code: `--font-mono` = `"SF Mono", "Fira Code", "JetBrains Mono", monospace`

**Sizing (all relative to `--font-size-base`):**

| Scale | Calc | Usage |
|-------|------|-------|
| Micro | `* 0.65` | Small badges |
| XS | `* 0.75` | Uppercase labels, caps |
| SM | `* 0.8` - `* 0.875` | Secondary text, body |
| Base | `* 1` | Default body |
| LG | `* 1.125` | Section headings |
| XL | `* 1.25` | Page headings |

Use `calc(var(--font-size-base) * N)` for custom CSS, or Tailwind `text-xs`/`text-sm`/`text-base`/`text-lg` classes.

**Weights:** 400 (normal), 500 (medium/active), 600 (semibold/headings), 700 (bold/emphasis)

**Line heights:** 1.2 (headings), 1.4 (small text), 1.5 (standard), 1.6 (body)

## Component Primitives

8 shared primitives in `web/src/components/chat/ui/`:

| Component | Base | Pattern |
|-----------|------|---------|
| `Button` | `<button>` | CVA variants: default, primary, destructive, outline, ghost. Sizes: sm, md, lg, icon |
| `Badge` | `<span>` | CVA variants: default, success, warning, error, info |
| `Dialog` | Radix Dialog | Portal + overlay + content wrapper |
| `Input` | `<input>` | Tailwind-styled |
| `Textarea` | `<textarea>` | Auto-resizing |
| `Select` | Radix Select | Full trigger/content/item wrapper |
| `ScrollArea` | `<div>` | Custom scrollbar styling |
| `Tooltip` | Radix Tooltip | Provider + trigger + content |

### Using CVA + cn()

```tsx
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '../../../lib/utils'

const myVariants = cva('base-classes', {
  variants: {
    variant: { default: '...', primary: '...' },
    size: { sm: '...', md: '...' },
  },
  defaultVariants: { variant: 'default', size: 'md' },
})

export function MyComponent({ className, variant, size, ...props }) {
  return <div className={cn(myVariants({ variant, size, className }))} {...props} />
}
```

Always use `cn()` to merge classes — it deduplicates Tailwind utilities.

## Styling Rules

### When to use what

| Use | When |
|-----|------|
| Tailwind classes | Default for all new styling |
| CSS custom properties | Theme-dependent values (colors, fonts, sizes) |
| Custom CSS in `*.css` files | Feature-scoped styles that need BEM naming, complex selectors, or keyframe animations |
| Inline styles | Never, except for dynamic values (e.g., calculated widths) |

### BEM for scoped CSS

Feature pages use BEM-like naming in co-located CSS files:

```css
/* PipelineEditor.css */
.pipeline-editor { ... }
.pipeline-editor__header { ... }
.pipeline-badge--completed { ... }
```

### Light mode overrides

When a component uses hardcoded dark-mode colors in CSS, add a light override in `web/src/styles/index.css`:

```css
/* Dark mode (default) */
.my-component { background: #052e16; color: #4ade80; }

/* Light mode override */
[data-theme="light"] .my-component { background: rgba(34, 197, 94, 0.12); color: #16a34a; }
```

Prefer CSS variables over hardcoded colors. Use `rgba()` with 0.08-0.12 opacity for tinted backgrounds in light mode.

### Tinted backgrounds

Use `color-mix()` for accent-tinted backgrounds:

```css
background: color-mix(in srgb, var(--accent) 10%, var(--bg-secondary));
```

## Icons

Inline SVG components — no icon library.

```tsx
// web/src/components/Icons.tsx
export function MyIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="..." />
    </svg>
  )
}
```

**Rules:**
- `viewBox="0 0 24 24"`, stroke-based, `fill="none"`
- Use `currentColor` to inherit parent color
- Default size 12-14px, accept `size` prop
- Add reusable icons to `Icons.tsx`, keep one-off icons inline

## Animations

**Standard timings:**

| Duration | Use |
|----------|-----|
| `0.1s` | Quick hover (background, color) |
| `0.15s ease` | Standard transitions (all properties) |
| `0.2s ease-out` | Panel/modal slide-in |

**Radix state-driven:**

```tsx
className="data-[state=open]:animate-in data-[state=closed]:animate-out
           data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0"
```

**Keyframe animations:** `spin` (0.8s), `pulse` (1.5s), `fadeIn` (0.2s), `slideIn` (0.2s)

Do NOT add animation libraries (framer-motion, GSAP, etc.).

## Z-Index Scale

| Z-Index | Layer |
|---------|-------|
| 1 | Minor overlays |
| 10 | Panels, terminal |
| 50 | Sidebar, source control |
| 100 | Dropdowns, modals, toasts |
| 200 | Full-screen overlays |
| 1000 | Error toasts, critical modals |

## State Management

- **Custom hooks** in `web/src/hooks/` — no Redux, Zustand, or Context API
- **Dual persistence:** localStorage (fast, offline) + API (authoritative)
- **Data fetching:** `fetch()` in hooks — no React Query, SWR, or Axios
- **WebSocket:** Ref-based connection in `useChat.ts` for streaming
- **Tab routing:** `useState<string>('chat')` in `App.tsx` — no React Router

## File Organization

```text
web/src/
├── components/           # PascalCase files
│   ├── chat/ui/         # Shared UI primitives
│   ├── chat/artifacts/  # Artifact renderers
│   ├── <feature>/       # Feature-scoped components
│   └── <PageName>.tsx   # Top-level page components
├── hooks/               # camelCase: use<Name>.ts
├── types/               # TypeScript type definitions
├── styles/              # Global CSS (index.css)
├── lib/                 # Utility functions (utils.ts)
└── App.tsx              # Root component with tab routing
```

**Naming:** PascalCase for components (`ChatPage.tsx`), camelCase for hooks (`useChat.ts`), kebab-case for CSS classes (`.code-block-header`).

## Anti-Patterns

Do NOT:
- Add new dependencies (UI libraries, state managers, routers, animation libs, icon packs)
- Use CSS-in-JS (styled-components, emotion)
- Use inline styles for anything except dynamic computed values
- Use raw hex colors — use CSS variables or Tailwind semantic names
- Use gradients — the design is solid colors only
- Add React Router or change the tab-based navigation
- Create global state providers (Context, Redux, Zustand)
- Use `!important` in custom CSS (Tailwind config already sets `important: true`)
- Skip light mode — every new themed style needs a `[data-theme="light"]` override
