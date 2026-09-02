import { act, renderHook, waitFor } from "@testing-library/react";
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

describe("useMcp templates", () => {
  it("fetches and caches the templates visible to the current project", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/mcp/templates?")) {
        return new Response(
          JSON.stringify({
            success: true,
            templates: [
              {
                name: "github",
                description: "GitHub template",
                owner: "gobby",
                scope: "global",
                params: [
                  {
                    name: "token",
                    required: true,
                    secret: true,
                    env: "GITHUB_TOKEN",
                    arg_flag: null,
                    choices: [],
                    description: "Token reference",
                  },
                ],
              },
            ],
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useMcp());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await result.current.fetchTemplates("project-1");
      await result.current.fetchTemplates("project-1");
    });

    expect(
      fetchMock.mock.calls.filter(([input]) =>
        String(input).includes("/api/mcp/templates?"),
      ),
    ).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/mcp/templates?scope=project&project_id=project-1",
    );
    await waitFor(() =>
      expect(result.current.templates).toEqual([
        expect.objectContaining({
          name: "github",
          scope: "global",
          params: [expect.objectContaining({ name: "token", secret: true })],
        }),
      ]),
    );
  });
});
