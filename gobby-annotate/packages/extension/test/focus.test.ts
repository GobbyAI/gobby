// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { isolateEditor, isolateFocus } from "../src/focus";

const cleanups: (() => void)[] = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
  document.documentElement.replaceChildren(document.head, document.body);
  document.body.replaceChildren();
  document.body.removeAttribute("style");
  document.documentElement.removeAttribute("style");
  document.body.inert = false;
  document.head.inert = false;
  vi.restoreAllMocks();
});

it("restores page styles, inert state and scroll after each editor session", async () => {
  const { host } = pageWithFocusTrap();
  document.body.inert = false;
  const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.spyOn(window, "scrollY", "get").mockReturnValue(240);
  document.body.style.setProperty("position", "sticky", "important");
  document.body.style.setProperty("top", "7px");
  document.documentElement.style.setProperty("overflow-x", "auto", "important");
  document.documentElement.style.setProperty("scroll-behavior", "smooth");
  host.style.pointerEvents = "none";
  const previousBody = document.body.style.cssText;
  const previousRoot = document.documentElement.style.cssText;
  for (let attempt = 0; attempt < 2; attempt++) {
    const release = isolateEditor(host);
    cleanups.push(release);
    expect(document.body.inert).toBe(true);
    expect(document.body.style.position).toBe("fixed");
    expect(document.body.style.top).toBe("-240px");
    expect(host.style.pointerEvents).toBe("auto");
    const banner = document.createElement("aside");
    banner.inert = attempt === 1;
    document.documentElement.append(banner);
    await Promise.resolve();
    expect(banner.inert).toBe(true);
    release();
    cleanups.pop();
    expect(document.body.inert).toBe(false);
    expect(banner.inert).toBe(attempt === 1);
    expect(document.body.style.cssText).toBe(previousBody);
    expect(document.documentElement.style.cssText).toBe(previousRoot);
    expect(host.style.pointerEvents).toBe("none");
    expect(scroll).toHaveBeenLastCalledWith(0, 240);
    banner.remove();
  }
});

it("keeps native input usable while shielding page gesture and bubbling input handlers", () => {
  const { host, textarea } = pageWithFocusTrap();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  const pageGesture = vi.fn(),
    pageInput = vi.fn(),
    edit = vi.fn();
  document.addEventListener("touchmove", pageGesture, true);
  document.addEventListener("input", pageInput);
  textarea.addEventListener("input", edit);
  cleanups.push(() =>
    document.removeEventListener("touchmove", pageGesture, true),
  );
  cleanups.push(() => document.removeEventListener("input", pageInput));
  const release = isolateEditor(host);
  cleanups.push(release);
  const inside = new Event("touchmove", {
    bubbles: true,
    composed: true,
    cancelable: true,
  });
  expect(textarea.dispatchEvent(inside)).toBe(true);
  expect(inside.defaultPrevented).toBe(false);
  expect(pageGesture).not.toHaveBeenCalled();
  const outside = new Event("touchmove", {
    bubbles: true,
    composed: true,
    cancelable: true,
  });
  expect(host.dispatchEvent(outside)).toBe(false);
  const backdropTap = new Event("touchstart", {
    bubbles: true,
    composed: true,
    cancelable: true,
  });
  expect(host.dispatchEvent(backdropTap)).toBe(false);
  const pointer = vi.fn();
  textarea.addEventListener("pointerdown", pointer);
  expect(
    textarea.dispatchEvent(
      new Event("pointerdown", {
        bubbles: true,
        composed: true,
        cancelable: true,
      }),
    ),
  ).toBe(true);
  expect(pointer).toHaveBeenCalledOnce();
  textarea.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  expect(edit).toHaveBeenCalledOnce();
  expect(pageInput).not.toHaveBeenCalled();
  release();
  cleanups.pop();
  textarea.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  expect(pageInput).toHaveBeenCalledOnce();
  textarea.dispatchEvent(
    new Event("touchmove", { bubbles: true, composed: true }),
  );
  expect(pageGesture).toHaveBeenCalledOnce();
});

it("releases toolbar dragging through the window listener while an editor is open", () => {
  const { host } = pageWithFocusTrap();
  const handle = document.createElement("button");
  host.shadowRoot!.append(handle);
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  cleanups.push(isolateEditor(host));
  const done = vi.fn();
  window.addEventListener("pointerup", done, { once: true });
  cleanups.push(() => window.removeEventListener("pointerup", done));
  handle.dispatchEvent(
    new Event("pointerup", { bubbles: true, composed: true }),
  );
  expect(done).toHaveBeenCalledOnce();
});

it("preserves existing page restrictions and unrelated changes when editing ends", () => {
  const { host } = pageWithFocusTrap();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  document.body.inert = true;
  document.body.style.setProperty("overflow-y", "scroll", "important");
  const release = isolateEditor(host);
  cleanups.push(release);
  document.body.style.color = "blue";
  release();
  cleanups.pop();
  expect(document.body.inert).toBe(true);
  expect(document.body.style.overflowY).toBe("scroll");
  expect(document.body.style.getPropertyPriority("overflow-y")).toBe(
    "important",
  );
  expect(document.body.style.color).toBe("blue");
});

// A page modal (Josh's iPhone report was over reddit's media lightbox) that
// pulls focus back whenever it lands outside it. Shadow DOM retargets the
// panel's focus events to the host, so the page sees focus leave the modal.
function pageWithFocusTrap() {
  const lightbox = document.createElement("div"),
    close = document.createElement("button");
  lightbox.append(close);
  document.body.append(lightbox);
  const host = document.createElement("div");
  const textarea = document.createElement("textarea");
  host.attachShadow({ mode: "open" }).append(textarea);
  document.documentElement.append(host);
  return { lightbox, close, host, textarea };
}

it("keeps the panel focused against a document focusin trap", () => {
  const { lightbox, close, host, textarea } = pageWithFocusTrap();
  const trap = (e: Event) => {
    if (!lightbox.contains(e.target as Node)) close.focus();
  };
  document.addEventListener("focusin", trap, true);
  cleanups.push(() => document.removeEventListener("focusin", trap, true));
  close.focus();
  cleanups.push(isolateFocus(host));

  textarea.focus();
  expect(host.shadowRoot!.activeElement).toBe(textarea);
  expect(document.activeElement).toBe(host);
});

it("hands focus events back to the page once released", () => {
  const { lightbox, close, host, textarea } = pageWithFocusTrap();
  const trap = (e: Event) => {
    if (!lightbox.contains(e.target as Node)) close.focus();
  };
  document.addEventListener("focusin", trap, true);
  cleanups.push(() => document.removeEventListener("focusin", trap, true));
  const seen: string[] = [];
  const record = (e: FocusEvent) => seen.push(e.type);
  document.addEventListener("focusin", record);
  cleanups.push(() => document.removeEventListener("focusin", record));

  const release = isolateFocus(host);
  textarea.focus();
  textarea.blur();
  expect(seen).toEqual([]);
  release();
  textarea.focus();
  expect(document.activeElement).toBe(close);
});
