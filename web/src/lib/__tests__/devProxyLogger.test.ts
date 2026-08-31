import { describe, expect, it, vi } from "vitest";
import type { Logger } from "vite";

import { proxyAwareErrorLogger } from "../devProxyLogger";

function codedError(code: string): Error {
  return Object.assign(new Error(code), { code });
}

describe("proxyAwareErrorLogger", () => {
  it.each([
    ["ws proxy error:\nError: read ECONNRESET", "ECONNRESET"],
    ["ws proxy socket error:\nError: write EPIPE", "EPIPE"],
  ])("suppresses expected websocket teardown %s", (message, code) => {
    const error = vi.fn<Logger["error"]>();
    const report = proxyAwareErrorLogger({ error });

    report(message, { error: codedError(code) });

    expect(error).not.toHaveBeenCalled();
  });

  it.each([
    ["http proxy error: /api", "EPIPE"],
    ["ws proxy error:", "ECONNREFUSED"],
    ["ws proxy error:", "ETIMEDOUT"],
  ])("retains actionable proxy failure %s %s", (message, code) => {
    const error = vi.fn<Logger["error"]>();
    const report = proxyAwareErrorLogger({ error });
    const options = { error: codedError(code) };

    report(message, options);

    expect(error).toHaveBeenCalledOnce();
    expect(error).toHaveBeenCalledWith(message, options);
  });
});
