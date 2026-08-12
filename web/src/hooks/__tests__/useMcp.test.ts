import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useMcp } from "../useMcp";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useMcp callTool", () => {
  it("returns the FastAPI detail for a non-OK response", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/api/mcp/tools/call")) {
        return new Response(JSON.stringify({ detail: "Tool is unavailable" }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useMcp());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await expect(
      result.current.callTool("server", "tool", {}),
    ).resolves.toEqual({
      success: false,
      error: "Tool is unavailable",
    });
  });
});
