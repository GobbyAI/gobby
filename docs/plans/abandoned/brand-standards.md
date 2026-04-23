# Gobby Brand Standards

> Comprehensive visual identity guide for the Gobby platform.
> Extracted from the live codebase — this is what's actually shipping.

---

## 1. Logo & Mascot

### Primary Logo
- **File:** `logo.png` (1024x1024 PNG, 169KB)
- **Character:** A green goblin-like creature with round glasses, holding a tablet/device
- **Background detail:** Circuit-board pattern with colored dots (purple, blue, teal) radiating outward — represents the "daemon that connects everything"
- **Primary mascot color:** Bright green (`~#6CBF2A` lime green body, dark outlines)
- **Usage:** App icon, marketing, social avatars

### "Built with Gobby" Badge
- **File:** `built-with-gobby.svg` (132x20 shield badge)
- **Left half:** `#555` (gray) — "built with"
- **Right half:** `#22c55e` (green-500) — "gobby"
- **Font:** Verdana, Geneva, DejaVu Sans
- **Text color:** White on gray, black on green
- **Usage:** README badges, project footers

---

## 2. Color System

### Design Philosophy
- **Dark-first** — dark mode is the default, light mode is the override
- **No gradients** — solid colors exclusively (the one exception: the SVG badge uses a subtle gradient overlay for depth)
- **Token-driven** — all colors are CSS custom properties, never hardcoded hex values in components
- **Semantic naming** — colors describe their *role*, not their hue

### Core Palette

| Token | Dark Mode | Light Mode | Role |
|-------|-----------|------------|------|
| `--bg-primary` | `#0a0a0a` | `#ffffff` | Page background |
| `--bg-secondary` | `#141414` | `#f5f5f5` | Cards, panels, sidebars |
| `--bg-tertiary` | `#1a1a1a` | `#e5e5e5` | Hover states, muted surfaces |
| `--text-primary` | `#e5e5e5` | `#171717` | Body text, headings |
| `--text-secondary` | `#a3a3a3` | `#525252` | Descriptions, secondary labels |
| `--text-muted` | `#737373` | `#a3a3a3` | Timestamps, hints, placeholders |
| `--accent` | `#3b82f6` | `#2563eb` | Links, active states, primary CTA |
| `--accent-hover` | `#2563eb` | `#1d4ed8` | Accent hover state |
| `--accent-foreground` | `#ffffff` | `#ffffff` | Text on accent backgrounds |
| `--border` | `#262626` | `#d4d4d4` | All borders and dividers |

### Accent Color: Blue-500
The brand accent is **Tailwind Blue-500** (`#3b82f6`). It's used for:
- Active navigation items
- Primary buttons
- Links
- Focus rings
- Selected/attached states (at 10% opacity as tint: `rgba(59, 130, 246, 0.1)`)

### Semantic / Status Colors

| Status | Dark BG | Dark FG | Light BG | Light FG |
|--------|---------|---------|----------|----------|
| **Success** | `#14532d` | `#22c55e` | `#dcfce7` | `#16a34a` |
| **Warning** | `#78350f` | `#f59e0b` | `#fef3c7` | `#d97706` |
| **Destructive** | `#7f1d1d` | `#f87171` | `#fecaca` | `#dc2626` |
| **Error** | — | `#f87171` | — | `#dc2626` |

### Status Badge Colors (Extended)

| Status | Dark BG | Dark FG | Light BG | Light FG |
|--------|---------|---------|----------|----------|
| Success/Active | `#052e16` | `#4ade80` | `rgba(34, 197, 94, 0.12)` | `#16a34a` |
| Error/Failed | `#450a0a` | `#f87171` | `rgba(239, 68, 68, 0.12)` | `#dc2626` |
| Warning/Pending | `#451a03` | `#fbbf24` | `rgba(245, 158, 11, 0.12)` | `#b45309` |
| Info/Running | `#0c4a6e` | `#38bdf8` | `rgba(59, 130, 246, 0.12)` | `#2563eb` |
| Agent/Purple | `#1e1b4b` | `#a78bfa` | `rgba(139, 92, 246, 0.12)` | `#7c3aed` |

**Light mode pattern:** Backgrounds use `rgba()` at **0.08–0.12 opacity** for a subtle tint, paired with a solid foreground.

### Message Role Colors

| Role | Dark BG | Light BG |
|------|---------|----------|
| User | `#1e3a5f` | `#dbeafe` |
| Assistant | `#1a1a1a` | `#f5f5f5` |
| System | `#2d1f1f` | `#fef2f2` |
| Code blocks | `#0d0d0d` | `#f0f0f0` |

### Priority Badge Colors (P0–P4)

| Priority | Background | Dark Text | Light Text |
|----------|-----------|-----------|------------|
| **P0** (Critical) | `rgba(239, 68, 68, 0.15)` | `#f87171` | `#dc2626` |
| **P1** (High) | `rgba(245, 158, 11, 0.15)` | `#fbbf24` | `#b45309` |
| **P2** (Medium) | `rgba(59, 130, 246, 0.12)` | `#60a5fa` | `#2563eb` |
| **P3** (Low) | `rgba(34, 197, 94, 0.12)` | `#4ade80` | `#16a34a` |
| **P4** (Nit) | `rgba(115, 115, 115, 0.15)` | `#a3a3a3` | `#737373` |

Pattern: `background: rgba(color, 0.12–0.15); color: <foreground>;` — light mode uses the same `rgba()` backgrounds but with darker foreground values.

### Transport Badge Colors (Dashboard)

| Transport | Background | Text |
|-----------|-----------|------|
| Internal | `rgba(34, 197, 94, 0.12)` | `#22c55e` |
| HTTP | `rgba(59, 130, 246, 0.12)` | `#60a5fa` |
| Stdio | `rgba(245, 158, 11, 0.12)` | `#fbbf24` |
| WebSocket | `rgba(139, 92, 246, 0.12)` | `#a78bfa` |
| SSE | `rgba(236, 72, 153, 0.12)` | `#f472b6` |

### Provider Session Dots

| Provider | Color | Hex |
|----------|-------|-----|
| Web Chat | Green | `#4ade80` |
| Claude | Orange | `#f97316` |
| Gemini | Blue | `#3b82f6` |
| Codex | Purple | `#a855f7` |
| Paused | Blue | `#3b82f6` |

### Session Indicator Colors

| Type | Color | Usage |
|------|-------|-------|
| User session | `#4ade80` (green-400) | Active user dot |
| Agent session | `#c084fc` (purple-400) | Agent dot + badge (`#2e1065` bg) |
| Dead session | `#737373` (neutral-500) | Inactive dot + badge (`#292524` bg) |

---

## 3. Typography

### Font Stacks

| Purpose | CSS Variable | Stack |
|---------|-------------|-------|
| **UI / Body** | `--font-sans` | `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif` |
| **Code / Mono** | `--font-mono` | `"SF Mono", "Fira Code", "JetBrains Mono", monospace` |

System fonts — no custom web fonts loaded. This keeps the UI fast and native-feeling on every platform.

### Type Scale

All sizes are relative to `--font-size-base` (default `16px`, user-adjustable 12–48px):

| Name | Multiplier | ~px @ 16px | Usage |
|------|-----------|------------|-------|
| Micro | `× 0.6` | 10px | Tiny count badges |
| XS | `× 0.75` | 12px | Uppercase labels, status pills |
| SM | `× 0.8` | 13px | Metadata, secondary text |
| Body SM | `× 0.875` | 14px | Standard body text |
| **Base** | `× 1` | **16px** | Default size |
| LG | `× 1.125` | 18px | Section headings |
| XL | `× 1.25` | 20px | Page titles |
| H3 | `1.25em` | — | Markdown H3 |
| H2 | `1.5em` | — | Markdown H2 |
| H1 | `1.75em` | — | Markdown H1 |

### Font Weights

| Weight | Value | Usage |
|--------|-------|-------|
| Normal | `400` | Body text |
| Medium | `500` | Active states, interactive labels, sidebar items |
| Semibold | `600` | Headings, badges, buttons |
| Bold | `700` | Strong emphasis only |

### Line Heights

| Value | Usage |
|-------|-------|
| `1.2` | Headings, badges |
| `1.4` | Small/secondary text |
| `1.5` | Standard content |
| `1.6` | Body text (global default) |

### Text Styling Rules
- **Uppercase + letter-spacing** (`0.05em`) for: status pills, section group labels
- **Monospace** for: code blocks, PIDs, terminal output, agent selectors
- **Truncation** with `text-overflow: ellipsis` for: session names, long labels

---

## 4. Spacing & Layout

### Spacing Scale (Tailwind 4px base unit)

| Tailwind | CSS | px | Usage |
|----------|-----|----|-------|
| `gap-1` | `0.25rem` | 4px | Minimum spacing |
| `gap-1.5` | `0.375rem` | 6px | Tight groups (icon + label) |
| `gap-2` | `0.5rem` | 8px | Standard element spacing |
| `gap-3` | `0.75rem` | 12px | Section element spacing |
| `gap-4` | `1rem` | 16px | Section padding, major gaps |
| `gap-6` | `1.5rem` | 24px | Page-level padding |

### Layout Dimensions

| Element | Value | Notes |
|---------|-------|-------|
| Sidebar width | `260px` | Collapses to `40px` on mobile |
| Chat max-width | `900px` | Centered with `margin: 0 auto` |
| Header padding | `1rem` | With `1px solid var(--border)` bottom |
| Terminal panel height | `300px` open | `2.75rem` collapsed |
| Interactive buttons | `2.25rem × 2.25rem` (36px) | Settings, hamburger |
| Small action buttons | `1.75rem × 1.75rem` (28px) | Inline actions |
| Micro action buttons | `1.25rem × 1.25rem` (20px) | Kill/delete (hidden until hover) |

### Border Radius Scale

| Value | px | Usage |
|-------|----|-------|
| `0.25rem` | 4px | Inline code, small elements, scrollbar thumb |
| `0.375rem` | 6px | Small buttons, inputs, dropdowns |
| `0.5rem` | 8px | Cards, messages, containers, panels |
| `9999px` | pill | Status badges, count indicators |
| `50%` | circle | Dot indicators, session dots (8px) |

---

## 5. Component Patterns

### Buttons (CVA Variants)

| Variant | Appearance | Use Case |
|---------|-----------|----------|
| `default` | Inverted (foreground bg, background text) | Standard actions |
| `primary` | Accent blue bg, white text | Primary CTA |
| `destructive` | Red bg, red text | Delete, remove |
| `outline` | Transparent + border | Secondary actions, cancel |
| `ghost` | Transparent, hover shows muted bg | Toolbar actions, minimal |

| Size | Dimensions |
|------|-----------|
| `sm` | `h-8 px-3 text-xs` (32px tall) |
| `md` | `h-9 px-4` (36px tall) — **default** |
| `lg` | `h-10 px-6 text-base` (40px tall) |
| `icon` | `h-9 w-9` (36px square) |

### Badges (CVA Variants)

| Variant | Style | Usage |
|---------|-------|-------|
| `default` | Muted bg, muted text | Neutral labels |
| `success` | Green bg/fg | Connected, completed |
| `warning` | Amber bg/fg | Pending, caution |
| `error` | Red bg/fg | Failed, errors |
| `info` | Accent at 20% opacity | Running, informational |

Shape: `rounded-full` (pill), `px-2.5 py-0.5`, `text-xs font-medium`.

### Status Pills (CSS)

```
font-size: 0.75× base
padding: 0.25rem 0.75rem
border-radius: 9999px
text-transform: uppercase
letter-spacing: 0.05em
```

On mobile (< 768px): collapse to 20×20px colored circles with arrow icons.

---

## 6. Icons

- **No icon library** — all icons are inline SVG components
- Standard `viewBox="0 0 24 24"`
- Stroke-based: `fill="none"`, `stroke="currentColor"`, `strokeWidth="2"`
- `strokeLinecap="round"`, `strokeLinejoin="round"`
- Default size: `12–14px`, accept optional `size` prop
- Icons inherit parent `color` via `currentColor`
- Shared icons in `web/src/components/shared/Icons.tsx`

---

## 7. Animation & Motion

### Transition Standards

| Duration | Easing | Usage |
|----------|--------|-------|
| `0.1s` | `ease` | Quick hover effects (bg, color changes) |
| `0.15s` | `ease` | Standard UI transitions (buttons, tabs, borders) |
| `0.2s` | `ease-out` | Panel entrance, slide-in, fade-in |
| `0.3s` | `ease` | Width transitions, layout shifts |

### Keyframe Animations

| Name | Duration | Usage |
|------|----------|-------|
| `spin` | `0.8s linear` | Loading spinners |
| `pulse` | `1.5s ease-in-out` | Tool call indicators |
| `fadeIn` | `0.2s ease` | Element appearance |
| `slideIn` | `0.2s ease-out` | Panel entrance from right |
| `pulse-recording` | `1.5s ease-in-out` | Recording indicator with ring |
| `speaking-wave` | `1.2s ease-in-out` | Voice speaking bars |

### Motion Policy
- Respects `prefers-reduced-motion` — all animations collapse to ~0ms
- No spring physics, no bounce, no parallax
- Hover reveal pattern: elements at `opacity: 0` → `opacity: 1` on parent hover (kill buttons, delete buttons)

---

## 8. Dark/Light Theme System

### How It Works
1. User selects theme in Settings (dark / light / system)
2. Stored in localStorage + API
3. Applied via `data-theme` attribute on `<html>`
4. CSS variables swap — **zero JS re-renders**
5. Tailwind classes resolve through CSS vars automatically

### Theme Switching Pattern
```css
/* Dark (default in :root) */
.my-element { background: #1a1a2e; color: #e0e0ff; }

/* Light override */
[data-theme="light"] .my-element { background: #f0f0ff; color: #1a1a2e; }
```

### Tinted Background Pattern
```css
/* Accent tint for selected states (modern — used in chat options) */
background: color-mix(in srgb, var(--accent) 10%, var(--bg-secondary));

/* Accent tint for active nav/attached items (legacy — rgba) */
background: rgba(59, 130, 246, 0.1);

/* Status badges — rgba with semantic foreground */
background: rgba(34, 197, 94, 0.12);
color: #22c55e;
```

**Prefer `color-mix()`** for new components — it respects theme switching automatically. The `rgba()` pattern is still used for status badges where the color is hardcoded by semantic meaning.

---

## 9. Z-Index Scale

| Z-Index | Layer | Examples |
|---------|-------|---------|
| `1` | Minor overlays | Subtle layering |
| `10` | Panels | Terminal panel, basic modals |
| `20` | Chat overlays | Floating chat elements |
| `50` | Sidebars | Navigation sidebar, source control |
| `60–61` | Sidebar + overlay | Overlay: 60, sidebar: 61 |
| `100` | Popovers | Dropdowns, modals, toasts |
| `200` | Full-screen | Full-screen overlays |
| `1000` | Critical | Error toasts, critical modals |

---

## 10. Responsive Breakpoints

| Breakpoint | Behavior |
|------------|----------|
| `> 768px` | Full sidebar, text status pills, full layout |
| `≤ 768px` | Sidebar collapses, status pills become colored dots, hamburger menu, mobile drawers |

### Mobile Adaptations
- Sidebar: slide-in overlay with backdrop (`rgba(0,0,0,0.4)`)
- Status pills: collapse to `20×20px` circles with `↑`/`↓` arrow content
- Touch: `touch-action: manipulation` on body
- Layout: `100dvh` for proper mobile viewport

---

## 11. Anti-Patterns (Do NOT)

| Don't | Do Instead |
|-------|-----------|
| Raw hex values (`bg-[#0a0a0a]`) | Semantic tokens (`bg-background`) |
| Gradients | Solid colors only |
| CSS-in-JS (styled-components) | Tailwind + CSS files |
| Inline styles (except computed values) | Tailwind classes or CSS vars |
| New icon libraries | Inline SVG with `currentColor` |
| `!important` in CSS | Tailwind `important: true` handles it |
| New state managers | Custom hooks in `hooks/` |
| React Router | Tab-based navigation via `useState` |
| Skip light mode | Always add `[data-theme="light"]` override |

---

## 12. Tech Stack Summary

| Layer | Technology |
|-------|-----------|
| Framework | React 18 + TypeScript 5 |
| Build | Vite 6 |
| Styling | Tailwind CSS 4 + CSS custom properties |
| Component variants | class-variance-authority (CVA) |
| Accessible primitives | Radix UI (Dialog, Select, Tabs, Tooltip) |
| Class merging | `cn()` = clsx + tailwind-merge |
| Code editing | CodeMirror 6 |
| Terminal | xterm.js 6 |
| Charts | Recharts + D3 |
| 3D/Graphs | Three.js + react-force-graph |

---

*This document reflects the live Gobby codebase as of March 12, 2026. Source of truth: `web/src/styles/index.css`, `web/src/components/chat/styles.css`, `web/src/components/dashboard/DashboardPage.css`, `web/src/components/tasks/tasks-page.css`, and `web/tailwind.config.ts`.*
