import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useAcpSessionDiscovery } from "../useAcpSessionDiscovery";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";

describe("useAcpSessionDiscovery (#17400)", () => {
  let mockFetch: MockFetchInstance;

  beforeEach(() => {
    vi.useFakeTimers();
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse("/api/sessions/acp/discover", {
      sessions: [],
      skipped: [],
      providers: [],
    });
  });

  afterEach(() => {
    mockFetch.restore();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("debounces the discover POST and fires once on panel open", async () => {
    renderHook(() => useAcpSessionDiscovery("live"));

    // Nothing fires until the debounce window elapses.
    expect(mockFetch.fn).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });

    expect(mockFetch.fn).toHaveBeenCalledTimes(1);
    expect(mockFetch.fn).toHaveBeenCalledWith(
      "/api/sessions/acp/discover",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("coalesces rapid segmented-control changes into a single discover", async () => {
    const { rerender } = renderHook(
      ({ mode }) => useAcpSessionDiscovery(mode),
      { initialProps: { mode: "live" } },
    );

    rerender({ mode: "expired" });
    rerender({ mode: "live" });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });

    // Only the trailing trigger survives the debounce window.
    expect(mockFetch.fn).toHaveBeenCalledTimes(1);
  });

  it("does not fire after the panel unmounts mid-debounce", async () => {
    const { unmount } = renderHook(() => useAcpSessionDiscovery("live"));
    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });

    expect(mockFetch.fn).not.toHaveBeenCalled();
  });

  it("guards against overlapping requests, queuing at most one pending re-run", async () => {
    // Drive the fetch promise directly so the first discover can stay in
    // flight while a new trigger arrives. The boundary cast mirrors the
    // project's own createMockFetch test helper.
    const resolvers: Array<() => void> = [];
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolvers.push(() => resolve(new Response("{}", { status: 200 })));
        }),
    );
    const original = globalThis.fetch;
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { rerender } = renderHook(
        ({ mode }) => useAcpSessionDiscovery(mode),
        { initialProps: { mode: "live" } },
      );

      // First discover fires after the debounce and then hangs (in flight).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(250);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);

      // A new trigger while the first is in flight must not start a second
      // overlapping request; it coalesces into a single pending re-run.
      rerender({ mode: "expired" });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(250);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);

      // Completing the in-flight request flushes exactly one pending re-run.
      await act(async () => {
        resolvers[0]?.();
        await Promise.resolve();
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      globalThis.fetch = original;
    }
  });
});
