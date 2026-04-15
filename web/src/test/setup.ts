import { vi } from 'vitest'
import '@testing-library/jest-dom/vitest'

// jsdom doesn't implement canvas — mock getContext to suppress warnings
HTMLCanvasElement.prototype.getContext = vi.fn().mockReturnValue(null)

// Node 25 ships a built-in `globalThis.localStorage` getter that warns about a
// missing `--localstorage-file` flag whenever it's touched. Vitest's jsdom env
// uses `populateGlobal()` to copy window props onto globalThis, but it skips
// keys that already exist there, so jsdom's own Storage never replaces Node's
// built-in. Reach into the dom that vitest stashes on `globalThis.jsdom` and
// pin both storages to jsdom's per-window instances.
const jsdomDom = (globalThis as { jsdom?: { window: Window } }).jsdom
if (jsdomDom?.window) {
  Object.defineProperty(globalThis, 'localStorage', {
    value: jsdomDom.window.localStorage,
    writable: true,
    configurable: true,
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    value: jsdomDom.window.sessionStorage,
    writable: true,
    configurable: true,
  })
}
