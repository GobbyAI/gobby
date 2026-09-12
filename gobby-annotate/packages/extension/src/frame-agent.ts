import type { Bounds, Viewport } from "@gobby/annotate-core";

export function viewport(win: Window = window): Viewport {
  const v = win.visualViewport;
  return {
    layout: { width: win.innerWidth, height: win.innerHeight },
    visual: {
      width: v?.width ?? win.innerWidth,
      height: v?.height ?? win.innerHeight,
      offsetLeft: v?.offsetLeft ?? 0,
      offsetTop: v?.offsetTop ?? 0,
      scale: v?.scale ?? 1,
    },
    devicePixelRatio: win.devicePixelRatio,
    scrollX: win.scrollX,
    scrollY: win.scrollY,
  };
}
export function locator(element: Element): string {
  if (element.id) return "#" + CSS.escape(element.id);
  const test = element.getAttribute("data-testid");
  if (test) return '[data-testid="' + CSS.escape(test) + '"]';
  const parts: string[] = [];
  let current: Element | null = element;
  while (current && parts.length < 12) {
    const siblings = current.parentElement
      ? [...current.parentElement.children].filter(
          (e) => e.localName === current!.localName,
        )
      : [current];
    parts.unshift(
      current.localName +
        ":nth-of-type(" +
        (siblings.indexOf(current) + 1) +
        ")",
    );
    current = current.parentElement;
  }
  return parts.join(" > ");
}
export type Hit = {
  element: Element;
  framePath: string[];
  shadowPath: string[];
  frameUrl: string;
  bounds: Bounds;
  topBounds: Bounds;
  hostOnly: boolean;
};
export function hitAt(
  x: number,
  y: number,
  doc: Document = document,
  framePath: string[] = [],
  transform = { x: 0, y: 0, sx: 1, sy: 1 },
): Hit | null {
  let element = doc.elementFromPoint(x, y);
  if (!element) return null;
  const shadowPath: string[] = [];
  while (element.shadowRoot) {
    const inner = element.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === element) break;
    shadowPath.push(locator(element));
    element = inner;
  }
  const rect = element.getBoundingClientRect();
  const bounds = {
    x: rect.x,
    y: rect.y,
    width: rect.width,
    height: rect.height,
  };
  let hostOnly = false;
  if (element.localName === "iframe") {
    const frame = element as HTMLIFrameElement;
    try {
      if (
        frame.contentDocument &&
        frame.contentWindow &&
        framePath.length < 32
      ) {
        const sx = rect.width / (frame.offsetWidth || rect.width),
          sy = rect.height / (frame.offsetHeight || rect.height);
        const ox = rect.x + frame.clientLeft * sx,
          oy = rect.y + frame.clientTop * sy;
        const inner = hitAt(
          (x - ox) / sx,
          (y - oy) / sy,
          frame.contentDocument,
          [...framePath, ...shadowPath, locator(frame)],
          {
            x: transform.x + ox * transform.sx,
            y: transform.y + oy * transform.sy,
            sx: transform.sx * sx,
            sy: transform.sy * sy,
          },
        );
        if (inner) return inner;
      }
      hostOnly = true;
    } catch {
      hostOnly = true;
    } // Cross-origin descendants are intentionally inaccessible.
  }
  // Closed roots cannot be detected reliably; custom elements are host evidence.
  if (element.localName.includes("-") && !element.shadowRoot) hostOnly = true;
  return {
    element,
    framePath,
    shadowPath,
    frameUrl: doc.URL,
    bounds,
    topBounds: {
      x: transform.x + rect.x * transform.sx,
      y: transform.y + rect.y * transform.sy,
      width: rect.width * transform.sx,
      height: rect.height * transform.sy,
    },
    hostOnly,
  };
}
