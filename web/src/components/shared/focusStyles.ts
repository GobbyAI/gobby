/*
 * Shared focus utility classes.
 *
 * These classes pair `focus:outline-none` with explicit `focus-visible`
 * outline utilities. Mouse focus stays quiet, while keyboard focus still gets
 * a visible 2px brand outline instead of relying on browser defaults.
 *
 * Use these constants on inputs and on icon-only or otherwise styled buttons
 * whenever a component needs to suppress the default focus outline.
 */

/** Inputs, textareas, selects: 2px brand outline on keyboard focus. */
export const inputFocusCls =
  'focus-visible:outline-2 focus-visible:outline-[var(--accent)] ' +
  'focus-visible:outline-offset-[1px] focus:outline-none'

/** Buttons: mirrors the `buttons.css` `:focus-visible` ring pattern. */
export const buttonFocusCls =
  'focus-visible:outline-2 focus-visible:outline-[var(--accent)] ' +
  'focus-visible:outline-offset-[2px] focus:outline-none'
