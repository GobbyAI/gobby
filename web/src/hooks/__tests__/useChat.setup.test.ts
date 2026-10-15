import { describe, it, expect } from "vitest";
import {
  cleanupUseChatTestContext,
  createUseChatTestContext,
  loadUseChatModule,
} from "./useChat.setup";

describe("useChat test context ownership", () => {
  it("loads useChat for the context that is still active", async () => {
    const context = createUseChatTestContext();
    try {
      await expect(loadUseChatModule()).resolves.toBeTypeOf("function");
    } finally {
      cleanupUseChatTestContext(context);
    }
  });

  it("rejects a load that outlives its context instead of rendering against the next test's mocks", async () => {
    const timedOut = createUseChatTestContext();
    const loading = loadUseChatModule();
    cleanupUseChatTestContext(timedOut);
    const next = createUseChatTestContext();
    try {
      await expect(loading).rejects.toThrow("outlived its test context");
    } finally {
      cleanupUseChatTestContext(next);
    }
  });
});
