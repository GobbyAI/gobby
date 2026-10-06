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

// Editing owns the interaction surface, but selection and ordinary browsing
// must keep the page usable. Inert also defeats traps installed on window
// before injection: their attempt to focus a page control cannot succeed.
export function isolateEditor(host: HTMLElement): () => void {
  const releaseFocus = isolateFocus(host);
  const restore: (() => void)[] = [];
  const position = { x: window.scrollX, y: window.scrollY };
  const inert = new Map<HTMLElement, boolean>();
  const disablePage = () => {
    for (const node of document.documentElement.children) {
      if (node instanceof HTMLElement && node !== host && !inert.has(node)) {
        inert.set(node, node.inert);
        node.inert = true;
      }
    }
  };
  disablePage();
  const observer = new MutationObserver(disablePage);
  observer.observe(document.documentElement, { childList: true });
  const set = (node: HTMLElement, property: string, value: string) => {
    const old = node.style.getPropertyValue(property);
    const priority = node.style.getPropertyPriority(property);
    const undo = () => {
      if (old) node.style.setProperty(property, old, priority);
      else node.style.removeProperty(property);
    };
    node.style.setProperty(property, value, "important");
    return undo;
  };
  const restoreScrollBehavior = set(
    document.documentElement,
    "scroll-behavior",
    "auto",
  );
  const styles: [HTMLElement, string, string][] = [
    [document.documentElement, "overflow-x", "hidden"],
    [document.documentElement, "overflow-y", "hidden"],
    [document.documentElement, "overscroll-behavior-x", "none"],
    [document.documentElement, "overscroll-behavior-y", "none"],
    [document.body, "position", "fixed"],
    [document.body, "top", `${-position.y}px`],
    [document.body, "left", `${-position.x}px`],
    [document.body, "width", "100%"],
    [document.body, "overflow-x", "hidden"],
    [document.body, "overflow-y", "hidden"],
    [host, "pointer-events", "auto"],
  ];
  for (const [node, property, value] of styles)
    restore.push(set(node, property, value));
  const gestures = [
    "touchstart",
    "touchmove",
    "touchend",
    "touchcancel",
    "wheel",
    "pointerdown",
    "mousedown",
  ];
  const stopGesture = (event: Event) => {
    if (event.target !== host) return;
    const backdrop = event.composedPath()[0] === host;
    if (
      (event.type === "pointerdown" || event.type === "mousedown") &&
      !backdrop
    )
      return;
    // Native input and inner scrolling remain enabled. Empty backdrop gestures
    // cannot pan the document or trigger a page's touch handlers.
    if (
      backdrop &&
      ["touchstart", "touchmove", "wheel", "pointerdown", "mousedown"].includes(
        event.type,
      )
    )
      event.preventDefault();
    event.stopImmediatePropagation();
  };
  for (const type of gestures)
    window.addEventListener(type, stopGesture, {
      capture: true,
      passive: false,
    });
  const shadow = host.shadowRoot;
  // Let pointerup reach window so toolbar drag cleanup can run.
  const inputs = [
    "keydown",
    "keyup",
    "keypress",
    "input",
    "beforeinput",
    "click",
    "pointerdown",
  ];
  const stopInput = (event: Event) => event.stopPropagation();
  for (const type of inputs) shadow?.addEventListener(type, stopInput);
  // React's autofocus can precede this layout effect; retry after the page is
  // inert. On iOS, a real tap still supplies the software-keyboard gesture.
  shadow
    ?.querySelector<HTMLTextAreaElement>("textarea")
    ?.focus({ preventScroll: true });
  return () => {
    observer.disconnect();
    for (const type of gestures)
      window.removeEventListener(type, stopGesture, true);
    for (const type of inputs) shadow?.removeEventListener(type, stopInput);
    releaseFocus();
    for (const [node, previous] of inert) node.inert = previous;
    // Restore body geometry before scrolling, with smooth scrolling disabled.
    for (const undo of restore.reverse()) undo();
    window.scrollTo(position.x, position.y);
    restoreScrollBehavior();
  };
}
