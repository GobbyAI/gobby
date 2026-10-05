const FOCUS_EVENTS = ["focusin", "focusout", "focus", "blur"] as const;

// Shadow DOM retargets the panel's focus events to the host, so a page modal's
// focus trap (reddit's media lightbox on iOS) sees focus leave the modal and
// pulls it back, and the comment box can never hold focus. Stopping our own
// focus events at the window, before the page's document-level listeners, keeps
// the panel invisible to such traps. Focus itself still moves: these events are
// notifications. A trap registered on the window before injection still runs.
export function isolateFocus(host: HTMLElement): () => void {
  const stop = (event: Event) => {
    if (event.target === host) event.stopImmediatePropagation();
  };
  for (const type of FOCUS_EVENTS) window.addEventListener(type, stop, true);
  return () => {
    for (const type of FOCUS_EVENTS)
      window.removeEventListener(type, stop, true);
  };
}
