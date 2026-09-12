import type { Bundle, Annotation } from "../src";
export const png = new Uint8Array(
  Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=",
    "base64",
  ),
);
export function fixture(): Bundle {
  const annotation: Annotation = {
    id: "10000000-0000-4000-8000-000000000001",
    revision: 1,
    createdAt: "2026-09-11T12:00:00.000Z",
    updatedAt: "2026-09-11T12:00:00.000Z",
    comment: "Increase button contrast",
    page: { url: "https://example.org/", title: "Example" },
    frame: { url: "https://example.org/", path: [], access: "document" },
    target: {
      kind: "element",
      locator: ["#save"],
      tag: "button",
      role: "button",
      text: "Save",
      bounds: { x: 10, y: 20, width: 44, height: 44 },
      screenshotBounds: { x: 10, y: 20, width: 44, height: 44 },
    },
    viewport: {
      layout: { width: 440, height: 956 },
      visual: {
        width: 440,
        height: 956,
        offsetLeft: 0,
        offsetTop: 0,
        scale: 1,
      },
      devicePixelRatio: 3,
      scrollX: 0,
      scrollY: 100,
    },
    screenshot: {
      status: "available",
      path: "screenshots/example.png",
      width: 1,
      height: 1,
    },
  };
  return {
    manifest: {
      version: 1,
      batchId: "20000000-0000-4000-8000-000000000001",
      exportId: "30000000-0000-4000-8000-000000000001",
      title: "Review",
      exportedAt: "2026-09-11T12:00:01.000Z",
      annotations: [annotation],
    },
    assets: new Map([["screenshots/example.png", png]]),
  };
}
