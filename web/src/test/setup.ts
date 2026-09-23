import { vi } from "vitest";
import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement canvas — mock getContext to suppress warnings
HTMLCanvasElement.prototype.getContext = vi.fn().mockReturnValue(null);

// jsdom doesn't implement matchMedia — stub to a desktop-default mock so any
// component using useIsMobile renders without crashing in tests. Tests that
// need to assert mobile behavior can override window.matchMedia per-suite.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

// jsdom loads no stylesheets, so the responsive tier tokens authored in
// styles/tailwind-theme.css are absent and every real useIsMobile render warns.
// Pin the authored values; suites that exercise other tokens set their own.
document.documentElement.style.setProperty(
  "--breakpoint-mobile-max-width",
  "767px",
);
document.documentElement.style.setProperty(
  "--breakpoint-mobile-max-height",
  "500px",
);

// jsdom builds its CSS cascade machinery (default stylesheet, selector and
// color engines) on the first getComputedStyle call, about 100 ms of CPU per
// test file. Pay it here, during environment setup, instead of inside
// whichever test first runs a role query or a user-event pointer check.
window.getComputedStyle(document.documentElement);

// Node 25 ships a built-in `globalThis.localStorage` getter that warns about a
// missing `--localstorage-file` flag whenever it's touched. Vitest's jsdom env
// uses `populateGlobal()` to copy window props onto globalThis, but it skips
// keys that already exist there, so jsdom's own Storage never replaces Node's
// built-in. Reach into the dom that vitest stashes on `globalThis.jsdom` and
// pin both storages to jsdom's per-window instances.
const jsdomDom = (globalThis as { jsdom?: { window: Window } }).jsdom;
if (jsdomDom?.window) {
  Object.defineProperty(globalThis, "localStorage", {
    value: jsdomDom.window.localStorage,
    writable: true,
    configurable: true,
  });
  Object.defineProperty(globalThis, "sessionStorage", {
    value: jsdomDom.window.sessionStorage,
    writable: true,
    configurable: true,
  });
}
