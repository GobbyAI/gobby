# Impeccable Craft — Web-Chat Approval Surface

## Context

The web-chat approval surface (tool-approval cards, plan-approval strip, and
the markdown/JSON they render) is off-spec and, in the user's words, "looks
like trash." Root cause is concrete and one-to-one locatable: **the web UI has
two parallel button systems and the approval cards are wired to the wrong
one.**

- The buttons the user likes — **New Chat, Hide Chat, Hide Panel, Close** —
  render through the CSS class system (`web/src/styles/buttons.css`) as
  `btn btn-accent btn-sm`: a **tinted** accent (10% `--accent-tint` fill,
  `--accent` text, ~35% accent border). Calm, bordered, legible — the
  "color is the fourth signal" posture `.impeccable.md` requires.
- The buttons the user hates render through the React CVA `Button`
  (`web/src/components/shared/buttonVariants.ts`) as `variant="primary"`
  (**solid `bg-accent`** slab) and `variant="destructive"` (**solid
  `bg-destructive`** magenta slab).

Symptoms, all traceable:

1. **Accent-slab overuse** (the already-documented code smell). The plan card
   stacks three solid-green slabs side by side; the tool-approval card's
   Approve is a solid slab. Violates the 60-30-10 rule and the skill's own
   "don't make every button primary."
2. **Inverted hierarchy.** Approve = loud green, Reject = loud magenta slab,
   Always Approve = bare ghost text — weight mapped backwards (the sticky
   "Always Approve" is quietest).
3. **Grayscale-fail.** Solid-green-vs-solid-magenta leans on hue+fill; the
   contract requires every state to survive desaturation.
4. **Raw JSON leak.** `JsonBlock.tsx:23` runs `JSON.stringify(value, null, 2)`
   on args, so a `plan` argument (itself markdown) renders as
   `"## Create…\n\n###…"` with literal `\n\n` and `\"`.
5. **Ransom-note inline code.** `CodeBlockRenderers.tsx:51` boxes inline
   `` `code` `` as a heavy `bg-muted` chip; plan lines become rows of chips
   interleaved with stray prose.
6. **Headings jam.** No heading override in the markdown renderer, so "Plan"
   sits flush against the following text.

**Intended outcome:** one button system as the single source of truth, the
tinted-accent language used everywhere the reference buttons use it, solid
fill reserved for the one dominant action per surface, a redesigned
plan-approval model built around execution mode, and approval content that
renders as real markdown.

**User decisions captured (binding):**
- **Scope: unify both button systems** (CSS `.btn-*` ↔ React CVA `Button`)
  into one source of truth across the web UI, then fix the cards.
- **Plan-approval model:** `Approve (YOLO)` as the **dominant** primary,
  `Approve (Act)` as the quieter secondary, `Reject` as a quiet destructive
  with an optional comment. YOLO is the comfortable default **because the
  rules engine + sandbox are the guardrails**. The approve button carries the
  post-plan execution mode, so the `/settings` "after Plan mode" preference is
  **removed**. (Memory `9719d4ec`.)

---

## 1. Unify the button system (one source of truth)

Make the React CVA `Button` (`web/src/components/shared/Button.tsx` +
`buttonVariants.ts`) the canonical component. Realign its variants to the
token semantics below, migrate the CSS `.btn-*` call sites, then retire the
button classes from `web/src/styles/buttons.css`.

**Variant semantics (tokens already exist in `web/src/styles/`):**

| Variant | Treatment | Use |
|---|---|---|
| `accent` *(new — replaces CSS `.btn-accent`)* | `--accent-tint` bg, `--accent` text, `color-mix(--accent 35%)` border | **Default action style.** New Chat, Hide Chat, Hide Panel, Close, Approve (tool), Approve (Act). |
| `primary` | solid `--accent` fill, `--accent-foreground` text | **Reserved: one dominant CTA per surface.** Approve (YOLO). |
| `destructive` *(realign: currently solid → make quiet)* | transparent bg, `--color-error` (magenta) text, `--color-error-soft` hover | Reject, destructive-quiet actions. |
| `outline` | `--border` border, transparent bg | Secondary (Always Approve). |
| `ghost` | transparent, muted hover | Tertiary / navigational (View). |
| `default` | `--foreground` / `--background` | unchanged. |

**Migration:** repoint the reference buttons and their neighbors from CSS
classes to `<Button variant="accent" size="sm">`, preserving exact
appearance:
- `web/src/components/chat/AgentStatusBar.tsx` (New Chat, ~163)
- `web/src/components/activity/ActivityPanel.tsx` (Hide Chat ~430, Close ~355,
  Hide Panel)
- status-bar / activity-panel call sites; drop the now-dead `.btn-*` rules in
  `web/src/styles/buttons.css` and `web/src/components/chat/styles/activity-panel.css`
  as their consumers migrate (in-place Tailwind-4 modernization per
  `.impeccable.md`).

This is the broadest part of the change; it touches many call sites by the
same mechanical swap. Migrate by surface, not all at once, but land them in
this effort so the fork closes.

---

## 2. Tool-approval card (`ToolCallCard.tsx` → `ToolApprovalCard`, ~394–461)

Rebuild the three-button row (~447–453) with real hierarchy. No solid slabs —
these cards stream frequently, so even Approve stays tinted:

- **Approve** → `variant="accent"` (tinted; reads as primary because the
  others are quieter)
- **Always Approve** → `variant="outline"` (bordered; more presence than bare
  text because it's the consequential, sticky choice)
- **Reject** → `variant="destructive"` (quiet magenta text)

Keep the warning-tinted card shell and the amber "Approval Required" badge —
that's correct state-palette usage. This card also fronts managed-CLI plan
mode (Droid's `ExitSpecMode` in the screenshots), so its quality matters for
plan flows too (see task #15637).

---

## 3. Plan-approval card (`PlanApprovalActions.tsx` + `PlanPendingActionStrip.tsx`)

Replace the dynamic, backend-populated permission-mode options (manually
approve / auto-accept / bypass) with a fixed three-action model:

- **Approve (YOLO)** → `variant="primary"` (the one dominant solid-accent CTA)
- **Approve (Act)** → `variant="accent"` (tinted secondary)
- **Reject** → `variant="destructive"` (quiet), opening the existing feedback
  input (`setShowFeedback(true)`) for an **optional** comment. This folds the
  old "Request Changes" / `keep_planning` ("Refine with Ultraplan") path into
  reject-with-comment — one consolidated negative action.
- **View** → keep as a small `ghost` text link, visually separated from the
  decision buttons (navigational, not a decision).

Remove the per-CLI `ApprovalOption[]` population and the
`decision: "approve" | "keep_planning"` branching for this card; the button
set is now fixed.

**Backend wiring** (`src/gobby/servers/websocket/handlers/plan_approval.py`,
`handle_plan_approval_response` / `_auto_continue_after_approval`): map the two
approve modes onto the existing two mechanisms (memory `2fb86ea6`):
- **Native Claude** (`plan_auto_switch=True`): Act → SDK permission mode
  `default`/`acceptEdits`; YOLO → `bypassPermissions`. The in-flight
  `ExitPlanMode` tool unblocks and the paused turn resumes.
- **Managed CLIs** (`plan_auto_switch=False`, e.g. Droid/Codex/Gemini): the
  injected continuation turn's simulated `chat_mode` carries Act vs YOLO.

**Remove** the `/settings` "what to do after switching out of Plan mode"
preference and its read sites — mode is now chosen at approval time. Locate it
during build (settings panel + wherever the post-plan preference is read in
the approval path) and delete it; the bottom `Plan | Act | YOLO`
SegmentedControl remains the live mode indicator.

---

## 4. Approval content rendering

1. **Kill the escaped-JSON leak.** In `ToolArgumentsContent` (in
   `ToolCallCard.tsx`) / `JsonBlock.tsx`: render multi-line **string** arg
   values with real newlines, and route markdown-bearing args (notably
   `plan`) through `MarkdownBody` instead of `JSON.stringify`. Reserve
   `JsonBlock` for genuine object/array args. The `plan` argument should read
   as a formatted plan, not an escaped blob.
2. **Lighten inline code** (`CodeBlockRenderers.tsx:51`): drop the heavy
   `bg-muted` chip treatment for inline `` `code` `` — keep `font-mono` with a
   faint tint or no fill so prose+code lines read as prose, not a row of
   boxed chips. Ghostty-grade restraint; true inline code still legible.
3. **Heading spacing** (`MarkdownBody.tsx` + heading components in
   `CodeBlock.tsx`): add `h1–h4` overrides with top margin and the
   `.impeccable.md` weight+size ladder so "Plan" no longer jams against
   "Files to create:".

---

## Files (representative)

- `web/src/components/shared/buttonVariants.ts`, `Button.tsx` — variant realign
- `web/src/styles/buttons.css`, `web/src/components/chat/styles/activity-panel.css` — retire `.btn-*`
- `web/src/components/chat/AgentStatusBar.tsx`, `web/src/components/activity/ActivityPanel.tsx` — migrate reference buttons
- `web/src/components/chat/ToolCallCard.tsx` — tool-approval hierarchy + args rendering
- `web/src/components/chat/PlanApprovalActions.tsx`, `PlanPendingActionStrip.tsx` — YOLO/Act/Reject model
- `web/src/components/chat/JsonBlock.tsx`, `CodeBlockRenderers.tsx`, `web/src/components/shared/MarkdownBody.tsx` — content rendering
- `src/gobby/servers/websocket/handlers/plan_approval.py` — mode wiring; settings panel — remove post-plan preference

**Monolith check:** `ToolCallCard.tsx` is large (logic past line 680). If any
touched source file lands over 1,000 lines, file/claim a refactor task per
CLAUDE.md rule 2 before closing. Related open work to reconcile, not
duplicate: **#15634** (approval placement), **#15637** (per-CLI ExitPlanMode
options), **#15638** (Plans panel fidelity).

---

## Verification

Craft Step 4 — visual iteration, both themes:

1. `uv run gobby start` + web dev server; open a chat.
2. **Tool approval:** trigger a Bash/tool call requiring approval. Confirm
   Approve (tinted) / Always Approve (outline) / Reject (quiet magenta)
   hierarchy; no solid slabs; args render with real newlines (no `\n\n`).
3. **Plan approval:** enter Plan mode, submit a plan, in **both** a native
   Claude session and a managed CLI (Droid) session. Confirm **Approve (YOLO)**
   is the dominant button, **Approve (Act)** secondary, **Reject** quiet with
   an optional comment; confirm approve actually continues execution in each
   path; confirm the `/settings` post-plan preference is gone.
4. **Content:** plan renders as markdown (headings spaced, inline code subtle,
   no ransom-note chips).
5. **Reference parity:** New Chat / Hide Chat / Hide Panel / Close are visually
   unchanged after migration to `<Button variant="accent">`.
6. **Contract checks:** grayscale-screenshot test (state legible without hue),
   focus rings on every button, keyboard parity, dark + light equal polish.
7. Targeted tests for any touched render/handler logic
   (`GOBBY_TEST_PROTECT=1 uv run pytest …`); lint + types
   (`uv run ruff check`, `uv run mypy src/`).
