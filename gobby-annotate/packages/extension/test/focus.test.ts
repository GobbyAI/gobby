// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest";
import { isolateFocus } from "../src/focus";

const cleanups: (() => void)[] = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
  document.documentElement.replaceChildren(document.head, document.body);
  document.body.replaceChildren();
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
