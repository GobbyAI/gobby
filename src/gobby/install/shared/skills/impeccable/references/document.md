> You are continuing a session under the `impeccable` skill; the design-context protocol and anti-pattern rules already apply.

# Document the project design contract

`document` maintains `.impeccable.md`, the project design contract owned by
impeccable's inline teach mode. It records the visual system already present in
the project so later work stays coherent. It does not create upstream product
documents or sidecars.

## 1. Load the contract

Read `.impeccable.md` and representative code, tokens, components, styles, and
assets. If the contract is missing, return to `SKILL.md`, complete `## Teach
Mode`, then resume this flow. Preserve the existing `## Design Context` section;
it contains user-confirmed product and brand truth.

If the contract conflicts with current code, show the user the conflict and ask
whether code or contract is authoritative. Never silently overwrite confirmed
guidance.

## 2. Extract current visual truth

Inspect sources in this order:

1. Design tokens, CSS custom properties, theme files, and framework config.
2. Shared components and their states, variants, accessibility behavior, and
   responsive rules.
3. Global styles, typography, color roles, spacing, shape, elevation, and motion.
4. Representative rendered surfaces when browser inspection is available.

Record only patterns the project actually uses. Preserve canonical values in
their native format and name their source paths. Omit absent systems instead of
inventing them.

## 3. Update `.impeccable.md`

Create or refresh one `## Design System` section with the smallest complete
description future agents need:

```markdown
## Design System

### Visual Direction
[Creative north star, density, material language, and explicit anti-patterns]

### Color
[Semantic roles, canonical values, contrast and appearance rules]

### Typography
[Families, roles, scale, line-height, and usage constraints]

### Layout and Spacing
[Container, grid, breakpoint, rhythm, and responsive behavior]

### Shape, Depth, and Motion
[Radius, borders, elevation, transitions, and reduced-motion behavior]

### Components
[Canonical components, variants, states, and source paths]

### Do and Avoid
[Project-specific rules grounded in the incumbent implementation]
```

Keep durable project-wide guidance here. Route-specific strategy stays in its
surface brief. Implementation details that change frequently stay in code.

## 4. Confirm

Review the updated section against at least one representative surface and the
canonical token/component sources. Report what was recorded, any unresolved
conflicts, and the source paths used. The flow is complete when
`.impeccable.md` describes the incumbent system without competing authorities.
