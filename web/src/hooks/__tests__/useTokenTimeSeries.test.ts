import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { createMockFetch, type MockFetchInstance } from "../../test/mocks/fetch";
import { useTokenTimeSeries } from "../useTokenTimeSeries";
import type { TimeSeriesGranularity } from "../../types/tokens";

let mockFetch: MockFetchInstance;

describe("useTokenTimeSeries", () => {
  beforeEach(() => {
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse("/api/admin/tokens/timeseries", {
      hours: 24,
      granularity: "1h",
      buckets: [],
    });
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  it("encodes granularity and project query parameters", async () => {
    renderHook(() =>
      useTokenTimeSeries(
        24,
        "proj/alpha",
        "1h&evil=1" as unknown as TimeSeriesGranularity,
      ),
    );

    await waitFor(() => {
      expect(mockFetch.fn).toHaveBeenCalled();
    });

    const [url] = mockFetch.fn.mock.calls[0] ?? [];
    expect(String(url)).toContain("granularity=1h%26evil%3D1");
    expect(String(url)).toContain("project_id=proj%2Falpha");
  });
});
