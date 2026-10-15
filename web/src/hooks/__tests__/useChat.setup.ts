import { vi } from "vitest";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../test/mocks/fetch";
import {
  createMockWebSocket,
  type MockWebSocketInstance,
} from "../../test/mocks/websocket";

export interface UseChatTestContext {
  mockWs: {
    instances: MockWebSocketInstance[];
    MockWebSocket: typeof WebSocket;
    restore: () => void;
  };
  mockFetch: MockFetchInstance;
  originalLocalStorage: Storage;
  consoleSpy: {
    log: ReturnType<typeof vi.spyOn>;
    debug: ReturnType<typeof vi.spyOn>;
    error: ReturnType<typeof vi.spyOn>;
    warn: ReturnType<typeof vi.spyOn>;
  };
}

// The context whose mocks currently own the globals. loadUseChatModule checks it
// once, after the dynamic import returns, and refuses to hand useChat to a test
// whose context was cleared or replaced while the module loaded. It does not
// isolate renders from a hook already returned to a test that later times out.
let activeContext: UseChatTestContext | null = null;

export function createUseChatTestContext(): UseChatTestContext {
  const consoleSpy = {
    log: vi.spyOn(console, "log").mockImplementation(() => {}),
    debug: vi.spyOn(console, "debug").mockImplementation(() => {}),
    error: vi.spyOn(console, "error").mockImplementation(() => {}),
    warn: vi.spyOn(console, "warn").mockImplementation(() => {}),
  };
  const mockWs = createMockWebSocket();
  const mockFetch = createMockFetch();
  const originalLocalStorage = globalThis.localStorage;

  // jsdom's localStorage does not delegate to Storage.prototype, so replace it.
  // Seed IDs so useChat mounts with the already-bound main web chat used at runtime.
  const store: Record<string, string> = {
    "gobby-conversation-id": "test-conversation-id",
    "gobby-db-session-id": "test-conversation-id",
  };
  const mockStorage = {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: vi.fn((key: string) => {
      delete store[key];
    }),
    clear: vi.fn(() => {
      Object.keys(store).forEach((key) => delete store[key]);
    }),
    key: vi.fn((_index: number) => null),
    get length() {
      return Object.keys(store).length;
    },
  };

  Object.defineProperty(globalThis, "localStorage", {
    value: mockStorage,
    writable: true,
    configurable: true,
  });

  const context = { mockWs, mockFetch, originalLocalStorage, consoleSpy };
  activeContext = context;
  return context;
}

export function cleanupUseChatTestContext(context: UseChatTestContext): void {
  if (activeContext === context) activeContext = null;
  context.mockWs.restore();
  context.mockFetch.restore();
  Object.defineProperty(globalThis, "localStorage", {
    value: context.originalLocalStorage,
    writable: true,
    configurable: true,
  });
  context.consoleSpy.log.mockRestore();
  context.consoleSpy.debug.mockRestore();
  context.consoleSpy.error.mockRestore();
  context.consoleSpy.warn.mockRestore();
  vi.restoreAllMocks();
}

export async function loadUseChatModule(): Promise<
  typeof import("../useChat").useChat
> {
  const owner = activeContext;
  vi.resetModules();
  const mod = await import("../useChat");
  if (owner === null || owner !== activeContext) {
    throw new Error(
      "useChat test outlived its test context (timed out); refusing to render against another test's mocks",
    );
  }
  return mod.useChat;
}
