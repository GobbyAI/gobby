import { vi } from 'vitest'

export interface MockLocalStorageInstance {
  store: Map<string, string>
  getItem: ReturnType<typeof vi.fn>
  setItem: ReturnType<typeof vi.fn>
  removeItem: ReturnType<typeof vi.fn>
  clear: ReturnType<typeof vi.fn>
  key: ReturnType<typeof vi.fn>
  readonly length: number
  restore: () => void
}

export function createMockLocalStorage(): MockLocalStorageInstance {
  const store = new Map<string, string>()
  const originalStorage = globalThis.localStorage

  const mock: MockLocalStorageInstance = {
    store,
    getItem: vi.fn((key: string) => store.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, String(value))
    }),
    removeItem: vi.fn((key: string) => {
      store.delete(key)
    }),
    clear: vi.fn(() => {
      store.clear()
    }),
    key: vi.fn((index: number) => {
      const keys = [...store.keys()]
      return keys[index] ?? null
    }),
    get length() {
      return store.size
    },
    restore() {
      Object.defineProperty(globalThis, 'localStorage', {
        value: originalStorage,
        writable: true,
        configurable: true,
      })
    },
  }

  Object.defineProperty(globalThis, 'localStorage', {
    value: mock,
    writable: true,
    configurable: true,
  })

  return mock
}
