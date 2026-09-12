import type { Annotation, Bounds } from "@gobby/annotate-core";
import { hitAt, locator, viewport } from "./frame-agent";
export type Mode = "browse" | "element" | "rectangle";
const captureChecks = new WeakMap<Annotation, () => boolean>();
const geometry = (element: Element) => {
  const { x, y, width, height } = element.getBoundingClientRect();
  return JSON.stringify({ x, y, width, height });
};
export function assertSelectionCurrent(annotation: Annotation): void {
  if (captureChecks.get(annotation)?.() === false)
    throw new Error(
      "Selection moved or its frame navigated. Select the target again.",
    );
}
export function contextText(element: Element): string {
  const walker = element.ownerDocument.createTreeWalker(
    element,
    NodeFilter.SHOW_TEXT,
  );
  let text = "",
    node: Node | null;
  while ((node = walker.nextNode()) && text.length < 500) {
    if (
      !node.parentElement?.closest(
        "input,textarea,select,script,style,[contenteditable]",
      )
    )
      text += node.textContent ?? "";
  }
  return text.replace(/\s+/g, " ").slice(0, 500);
}
export function makeAnnotation(
  rect: Bounds,
  hit: ReturnType<typeof hitAt> = null,
): Annotation {
  const now = new Date().toISOString(),
    view = viewport();
  const target = hit?.topBounds ?? rect;
  // Text values in editable controls are deliberately excluded, including descendants.
  const text = hit ? contextText(hit.element) : "";
  const annotation: Annotation = {
    id: crypto.randomUUID(),
    revision: 1,
    createdAt: now,
    updatedAt: now,
    comment: "",
    page: { url: location.href, title: document.title.slice(0, 2000) },
    frame: {
      url: hit?.frameUrl ?? location.href,
      path: hit?.framePath ?? [],
      access: hit
        ? hit.hostOnly
          ? "host-only"
          : "document"
        : "visible-region",
    },
    target: {
      kind: hit ? "element" : "rectangle",
      locator: hit ? [...hit.shadowPath, locator(hit.element)] : [],
      tag: hit?.element.localName ?? "",
      role: hit?.element.getAttribute("role") ?? "",
      text,
      bounds: hit?.bounds ?? rect,
      screenshotBounds: {
        ...target,
        x: target.x - view.visual.offsetLeft,
        y: target.y - view.visual.offsetTop,
      },
    },
    viewport: view,
    screenshot: { status: "unavailable", reason: "Capture pending" },
  };
  if (hit) {
    const element = hit.element,
      initialBounds = geometry(element);
    const checks: (() => boolean)[] = [
      () => element.isConnected && geometry(element) === initialBounds,
    ];
    let doc: Document | null = element.ownerDocument;
    while (doc?.defaultView) {
      const capturedDocument: Document = doc,
        win: Window = doc.defaultView;
      const initialViewport = JSON.stringify(viewport(win)),
        url = doc.URL;
      checks.push(
        () =>
          win.document === capturedDocument &&
          capturedDocument.URL === url &&
          JSON.stringify(viewport(win)) === initialViewport,
      );
      const frame: Element | null = win.frameElement;
      if (!frame) break;
      const frameBounds = geometry(frame);
      checks.push(() => frame.isConnected && geometry(frame) === frameBounds);
      doc = frame.ownerDocument;
    }
    captureChecks.set(annotation, () => checks.every((check) => check()));
  }
  return annotation;
}
export function select(
  mode: Exclude<Mode, "browse">,
  host: HTMLElement,
  onSelected: (a: Annotation) => void,
  onCancel: () => void,
): () => void {
  const overlay = document.createElement("div");
  overlay.tabIndex = 0;
  overlay.setAttribute("role", "region");
  overlay.setAttribute(
    "aria-label",
    `Select ${mode}. Move with arrow keys, Shift for larger steps. Enter confirms${mode === "rectangle" ? " each corner" : ""}. Escape cancels.`,
  );
  Object.assign(overlay.style, {
    position: "fixed",
    inset: "0",
    zIndex: "2147483646",
    cursor: "crosshair",
    touchAction: "none",
  });
  const highlight = document.createElement("div");
  Object.assign(highlight.style, {
    position: "fixed",
    border: "2px solid #c1db55",
    background: "#c1db5522",
    pointerEvents: "none",
    boxSizing: "border-box",
  });
  overlay.append(highlight);
  document.documentElement.append(overlay);
  let start: { x: number; y: number } | null = null;
  const view = viewport().visual;
  let point = {
    x: view.offsetLeft + view.width / 2,
    y: view.offsetTop + view.height / 2,
  };
  let stopped = false;
  const originalUrl = location.href,
    previousFocus = document.activeElement;
  let animation = 0;
  const hit = () => {
    overlay.style.pointerEvents = "none";
    const result = hitAt(point.x, point.y);
    overlay.style.pointerEvents = "";
    return result;
  };
  const rectangle = (): Bounds => ({
    x: Math.min(start?.x ?? point.x, point.x),
    y: Math.min(start?.y ?? point.y, point.y),
    width: Math.abs(point.x - (start?.x ?? point.x)),
    height: Math.abs(point.y - (start?.y ?? point.y)),
  });
  const paint = () => {
    const rect = mode === "element" ? hit()?.topBounds : rectangle();
    if (rect)
      Object.assign(highlight.style, {
        left: rect.x + "px",
        top: rect.y + "px",
        width: rect.width + "px",
        height: rect.height + "px",
      });
  };
  const prevent = (e: Event) => {
    e.preventDefault();
    e.stopImmediatePropagation();
  };
  const down = (e: PointerEvent) => {
    prevent(e);
    point = { x: e.clientX, y: e.clientY };
    start = point;
    overlay.setPointerCapture(e.pointerId);
    paint();
  };
  const move = (e: PointerEvent) => {
    prevent(e);
    point = { x: e.clientX, y: e.clientY };
    paint();
  };
  const commit = () => {
    const target = mode === "element" ? hit() : null,
      rect = rectangle();
    if (
      (mode === "element" && !target) ||
      (mode === "rectangle" && (rect.width < 2 || rect.height < 2))
    )
      return;
    const annotation = makeAnnotation(rect, target);
    // Keep the overlay through the synthesized click, preventing activation below it.
    setTimeout(() => {
      if (stopped) return;
      cleanup();
      onSelected(annotation);
    }, 0);
  };
  const up = (e: PointerEvent) => {
    prevent(e);
    point = { x: e.clientX, y: e.clientY };
    commit();
  };
  const key = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      prevent(e);
      cleanup();
      onCancel();
      return;
    }
    if (document.activeElement !== overlay) return;
    if (e.key.startsWith("Arrow")) {
      prevent(e);
      const step = e.shiftKey ? 10 : 1,
        visible = viewport().visual;
      point.x = Math.max(
        visible.offsetLeft,
        Math.min(
          visible.offsetLeft + visible.width - 1,
          point.x +
            (e.key === "ArrowRight" ? step : e.key === "ArrowLeft" ? -step : 0),
        ),
      );
      point.y = Math.max(
        visible.offsetTop,
        Math.min(
          visible.offsetTop + visible.height - 1,
          point.y +
            (e.key === "ArrowDown" ? step : e.key === "ArrowUp" ? -step : 0),
        ),
      );
      paint();
    } else if (e.key === "Enter") {
      prevent(e);
      if (mode === "rectangle" && !start) start = { ...point };
      else commit();
    }
  };
  const cancel = () => {
    cleanup();
    onCancel();
  };
  const cleanup = () => {
    if (stopped) return;
    stopped = true;
    cancelAnimationFrame(animation);
    const restoreFocus = document.activeElement === overlay;
    overlay.remove();
    if (
      restoreFocus &&
      previousFocus instanceof HTMLElement &&
      previousFocus.isConnected
    )
      previousFocus.focus({ preventScroll: true });
    window.removeEventListener("keydown", key, true);
    window.removeEventListener("pagehide", cancel);
    window.removeEventListener("popstate", cancel);
    window.removeEventListener("scroll", paint, true);
    host.removeEventListener("annotate-cancel", cancel);
  };
  overlay.addEventListener("pointerdown", down);
  overlay.addEventListener("pointermove", move);
  overlay.addEventListener("pointerup", up);
  overlay.addEventListener("click", prevent);
  overlay.addEventListener("pointercancel", cancel);
  window.addEventListener("keydown", key, true);
  window.addEventListener("pagehide", cancel);
  window.addEventListener("popstate", cancel);
  window.addEventListener("scroll", paint, true);
  host.addEventListener("annotate-cancel", cancel);
  const tick = () => {
    if (location.href !== originalUrl) {
      cancel();
      return;
    }
    paint();
    animation = requestAnimationFrame(tick);
  };
  overlay.focus({ preventScroll: true });
  animation = requestAnimationFrame(tick);
  return cleanup;
}
