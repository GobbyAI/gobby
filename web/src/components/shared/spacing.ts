/**
 * Spacing tokens for chat message + tool-call rhythm.
 *
 * These bundle the layout/padding/gap utilities that make a tool card
 * a tool card and a message body a message body, so the rhythm between
 * the two surfaces stays in one place. Callers compose with `cn(...)`
 * and add typography/color/state classes alongside (e.g. `text-sm`,
 * `cursor-pointer`, `bg-accent/5`).
 */

/** Spacing for the chat message container and its top-level header rows. */
export const MESSAGE_SPACING = {
  /** Outer message body padding (`MessageItem` container). */
  body: 'px-4 py-3',
  /** Header strip with role label + timestamp. */
  headerRow: 'flex items-center gap-2 mb-1.5',
  /** Spinner / metadata strip below the header. */
  metaRow: 'flex items-center gap-2 py-2',
} as const

/** Spacing for tool-call cards (collapsed header + expanded body + labels). */
export const TOOL_CARD_SPACING = {
  /** Clickable header row of a collapsed tool card. */
  header: 'flex items-center gap-2 px-3 py-1.5',
  /** Slightly denser header row (group rollups, approval/info rows). */
  headerDense: 'flex items-center gap-2 px-3 py-2',
  /** Expanded body section: top border + padding + vertical rhythm. */
  body: 'border-t border-border px-3 py-2 space-y-2',
  /** Body-section padding without a top border (collapsed-but-shown body). */
  bodyCompact: 'px-3 pb-2',
  /** Small label inside an expanded section ("Arguments", "Result"). */
  label: 'mb-1 font-medium',
} as const
