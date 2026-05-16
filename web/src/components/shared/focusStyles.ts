/*
 * Shared focus utility classes.
 *
 * The base @layer in `web/src/styles/index.css` already paints a 2px
 * `outline: var(--accent)` on every interactive element with `:focus-visible`.
 * The Tailwind hop `focus:outline-none` (without an accompanying ring or
 * border treatment) defeats that base layer and leaves keyboard users
 * with no focus indicator — a WCAG 2.4.7 / 2.4.11 violation.
 *
 * Use these constants on inputs and on icon-only or otherwise styled
 * buttons so the explicit intent is "the base outline is fine, but I am
 * adding a visible focus-visible affordance on top." They keep the
 * outline silent on mouse focus (focus-visible only) and tighten the
 * offset from the base 2px to 1px for input chrome.
 */

/** Inputs, textareas, selects: 2px brand outline on keyboard focus. */
export const inputFocusCls =
  'focus-visible:outline-2 focus-visible:outline-[var(--accent)] ' +
  'focus-visible:outline-offset-[1px] focus:outline-none'

/** Buttons: mirrors the `buttons.css` `:focus-visible` ring pattern. */
export const buttonFocusCls =
  'focus-visible:outline-2 focus-visible:outline-[var(--accent)] ' +
  'focus-visible:outline-offset-[2px] focus:outline-none'
