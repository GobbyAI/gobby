import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createMockFetch,
  type MockFetchInstance,
} from "../../test/mocks/fetch";
import { usePipelineDefs } from "../usePipelineDefs";
import { useRules } from "../useRules";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

let mockFetch: MockFetchInstance;

beforeEach(() => {
  mockFetch = createMockFetch();
});

afterEach(() => {
  mockFetch.restore();
  vi.restoreAllMocks();
});

describe("filtered hook refetches", () => {
  it("preserves pipeline filters when a mutation refetches the list", async () => {
    const filteredUrl = "/api/pipelines/definitions?include_deleted=true";
    mockFetch.mockJsonResponse(/\/api\/pipelines\/definitions$/, {
      definitions: [],
    });
    mockFetch.mockJsonResponse(filteredUrl, { definitions: [] });
    mockFetch.mockJsonResponse("/api/pipelines/definitions/pipeline-1/toggle", {
      status: "success",
      definition: { id: "pipeline-1" },
    });

    const { result } = renderHook(() => usePipelineDefs());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(() =>
      result.current.fetchPipelines({
        include_deleted: true,
      }),
    );
    await act(() => result.current.toggleEnabled("pipeline-1"));

    const filteredCalls = mockFetch.fn.mock.calls.filter(
      ([url]) => String(url) === filteredUrl,
    );
    expect(filteredCalls).toHaveLength(2);
  });

  it("preserves rule filters when a mutation refetches the list", async () => {
    const filteredUrl = "/api/rules?group=project&enabled=false";
    mockFetch.mockJsonResponse(/\/api\/rules$/, { rules: [] });
    mockFetch.mockJsonResponse("/api/rules/groups", { groups: [] });
    mockFetch.mockJsonResponse(filteredUrl, { rules: [] });
    mockFetch.mockJsonResponse("/api/rules/project-rule/toggle", {
      status: "success",
    });

    const { result } = renderHook(() => useRules());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(() =>
      result.current.fetchRules({ group: "project", enabled: false }),
    );
    await act(() => result.current.toggleRule("project-rule", true));

    const filteredCalls = mockFetch.fn.mock.calls.filter(
      ([url]) => String(url) === filteredUrl,
    );
    expect(filteredCalls).toHaveLength(2);
  });
});
