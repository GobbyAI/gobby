import type { MouseEvent } from "react";

/**
 * Keeps the terminal focused when one of the pane's own controls is clicked.
 *
 * Focus is what holds the daemon's writer lease: leaving the pane releases it.
 * A control that acts *on* the terminal — a quick key, a write retry — would
 * otherwise take focus on mousedown, release the lease, and then race its own
 * write against that release, so the very keystroke the button exists to send
 * comes back refused. Preventing the default mousedown leaves focus where it
 * is; keyboard activation still moves focus normally, which is what a user
 * tabbing away actually means.
 */
export function keepTerminalFocus(event: MouseEvent<HTMLElement>): void {
  event.preventDefault();
}
