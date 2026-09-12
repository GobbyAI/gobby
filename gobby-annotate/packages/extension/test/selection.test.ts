// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import {
  assertSelectionCurrent,
  contextText,
  makeAnnotation,
  select,
} from "../src/selection";
it("omits editable descendants and form values from semantic text", () => {
  const el = document.createElement("div");
  el.innerHTML =
    '<p>Safe</p><textarea>secret</textarea><input value="private"><div contenteditable="true">hidden note</div>';
  expect(contextText(el)).toBe("Safe");
});
it("records CSS coordinates and viewport scroll separately", () => {
  Object.defineProperty(window, "scrollY", { configurable: true, value: 120 });
  const a = makeAnnotation({ x: 10, y: 20, width: 80, height: 90 });
  expect(a.target.bounds).toEqual({ x: 10, y: 20, width: 80, height: 90 });
  expect(a.target.screenshotBounds).toEqual(a.target.bounds);
  expect(a.viewport.scrollY).toBe(120);
  expect(a.frame.access).toBe("visible-region");
});
it("Escape removes interception without activating a page control", () => {
  const host = document.createElement("div");
  let cancelled = false,
    selected = false;
  const cleanup = select(
    "rectangle",
    host,
    () => {
      selected = true;
    },
    () => {
      cancelled = true;
    },
  );
  const event = new KeyboardEvent("keydown", {
    key: "Escape",
    cancelable: true,
  });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  expect(cancelled).toBe(true);
  expect(selected).toBe(false);
  expect(document.querySelector('[style*="crosshair"]')).toBeNull();
  cleanup();
});
it("rejects nested frame target movement and replacement before attaching evidence", () => {
  const frame = document.createElement("iframe");
  document.body.append(frame);
  const element = frame.contentDocument!.createElement("button");
  frame.contentDocument!.body.append(element);
  let y = 10;
  vi.spyOn(element, "getBoundingClientRect").mockImplementation(
    () => new DOMRect(10, y, 30, 40),
  );
  const bounds = { x: 10, y, width: 30, height: 40 };
  const annotation = makeAnnotation(bounds, {
    element,
    bounds,
    topBounds: bounds,
    framePath: ["iframe"],
    shadowPath: [],
    frameUrl: frame.contentDocument!.URL,
    hostOnly: false,
  });
  expect(() => assertSelectionCurrent(annotation)).not.toThrow();
  y = 20;
  expect(() => assertSelectionCurrent(annotation)).toThrow(/Selection moved/);
  y = 10;
  element.remove();
  expect(() => assertSelectionCurrent(annotation)).toThrow(/Selection moved/);
  frame.remove();
  vi.restoreAllMocks();
});
it("selects rectangle corners with the keyboard without page activation", () => {
  vi.useFakeTimers();
  const selected = vi.fn();
  const cleanup = select(
    "rectangle",
    document.createElement("div"),
    selected,
    vi.fn(),
  );
  try {
    for (const key of [
      "Enter",
      "ArrowRight",
      "ArrowRight",
      "ArrowDown",
      "ArrowDown",
      "Enter",
    ]) {
      const event = new KeyboardEvent("keydown", { key, cancelable: true });
      window.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
    }
    vi.advanceTimersByTime(1);
    expect(selected).toHaveBeenCalledOnce();
    expect(selected.mock.calls[0]![0].target.bounds).toMatchObject({
      width: 2,
      height: 2,
    });
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});
