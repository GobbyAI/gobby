// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import {
  captureHidden,
  regionCrop,
  sameCapture,
  type Fingerprint,
} from "../src/screenshots";
import { fixture } from "../../core/test/fixtures";
it("maps regions to native pixels using each actual screenshot axis", () => {
  const view = fixture().manifest.annotations[0]!.viewport;
  view.visual = {
    width: 400,
    height: 300,
    offsetLeft: 37,
    offsetTop: 51,
    scale: 2,
  };
  view.scrollX = 100;
  view.scrollY = 200;
  view.devicePixelRatio = 3;
  expect(
    regionCrop({ x: 10.25, y: 20.5, width: 30, height: 40 }, view, 800, 900),
  ).toEqual({
    x: 20,
    y: 61,
    width: 61,
    height: 121,
    sourceBounds: { x: 10, y: 61 / 3, width: 30.5, height: 121 / 3 },
  });
  expect(
    regionCrop({ x: -10, y: 280, width: 30, height: 40 }, view, 800, 900),
  ).toEqual({
    x: 0,
    y: 840,
    width: 40,
    height: 60,
    sourceBounds: { x: 0, y: 280, width: 20, height: 20 },
  });
  expect(() =>
    regionCrop({ x: 410, y: 10, width: 20, height: 20 }, view, 800, 900),
  ).toThrow("outside the screenshot");
});
it("hides the UI through capture and restores it on failure", async () => {
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  const host = document.createElement("div");
  host.style.visibility = "visible";
  await expect(
    captureHidden(host, async () => {
      expect(host.style.visibility).toBe("hidden");
      throw new Error("Denied");
    }),
  ).rejects.toThrow("Denied");
  expect(host.style.visibility).toBe("visible");
  vi.unstubAllGlobals();
});
it("accepts capture state after messaging reorders nested keys", () => {
  const before: Fingerprint = {
    document: "one",
    url: "https://example.org",
    viewport: fixture().manifest.annotations[0]!.viewport,
  };
  const v = before.viewport;
  const received: Fingerprint = {
    viewport: {
      scrollY: v.scrollY,
      scrollX: v.scrollX,
      devicePixelRatio: v.devicePixelRatio,
      visual: {
        scale: v.visual.scale,
        offsetTop: v.visual.offsetTop,
        offsetLeft: v.visual.offsetLeft,
        height: v.visual.height,
        width: v.visual.width,
      },
      layout: { height: v.layout.height, width: v.layout.width },
    },
    url: before.url,
    document: before.document,
  };
  expect(JSON.stringify(received)).not.toBe(JSON.stringify(before));
  expect(sameCapture(before, received)).toBe(true);
  for (const key of ["scrollX", "scrollY", "devicePixelRatio"] as const) {
    const changed = structuredClone(received);
    changed.viewport[key] += 0.01;
    expect(sameCapture(before, changed), key).toBe(false);
  }
  for (const group of ["layout", "visual"] as const) {
    for (const key of Object.keys(received.viewport[group])) {
      const changed = structuredClone(received);
      const values = changed.viewport[group] as Record<string, number>;
      values[key] = values[key]! + 0.01;
      expect(sameCapture(before, changed), `${group}.${key}`).toBe(false);
    }
  }
});
it("rejects viewport, document and navigation drift", () => {
  const before = {
    document: "one",
    url: "https://example.org",
    viewport: fixture().manifest.annotations[0]!.viewport,
  };
  expect(sameCapture(before, structuredClone(before))).toBe(true);
  expect(sameCapture(before, { ...before, document: "two" })).toBe(false);
  expect(sameCapture(before, { ...before, url: "https://other.org" })).toBe(
    false,
  );
  expect(
    sameCapture(before, {
      ...before,
      viewport: { ...before.viewport, scrollY: 101 },
    }),
  ).toBe(false);
});
