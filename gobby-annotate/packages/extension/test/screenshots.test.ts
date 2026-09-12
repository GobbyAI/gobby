// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import { captureHidden, sameCapture } from "../src/screenshots";
import { fixture } from "../../core/test/fixtures";
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
