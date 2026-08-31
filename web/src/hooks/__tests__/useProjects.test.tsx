import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useProjects, type ProjectWithStats } from "../useProjects";
import { useWebSocketEvent } from "../useWebSocketEvent";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

// #20066: on a fresh unauthenticated load the mount fetch 401s and nothing
// retried after login, so the project list stayed empty until a project_event
// happened to arrive. The `enabled` option must defer fetching while false and
// re-run the fetch when it flips true.

function makeProject(
  overrides: Partial<ProjectWithStats> = {},
): ProjectWithStats {
  return {
    id: "p1",
    name: "gobby",
    display_name: "Gobby",
    checkout: {
      machine_id: "machine-1",
      root_path: "/Users/josh/Projects/gobby",
    },
    github_url: null,
    github_repo: null,
    linear_team_id: null,
    linear_project_id: null,
    approval_rules: [],
    validation_detection: null,
    created_at: "2026-08-11T00:00:00Z",
    updated_at: "2026-08-11T00:00:00Z",
    session_count: 0,
    open_task_count: 0,
    last_activity_at: null,
    ...overrides,
  };
}

function okResponse(projects: ProjectWithStats[]): Response {
  return { ok: true, json: async () => projects } as Response;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("useProjects auth gating (#20066)", () => {
  it("fetches on mount by default", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(okResponse([makeProject()])),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useProjects());

    await waitFor(() => expect(result.current.allProjects).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/projects");
  });

  it("defers fetching while disabled and fetches once enabled flips true", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okResponse([makeProject()])));
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useProjects({ enabled }),
      { initialProps: { enabled: false } },
    );
    expect(fetchMock).not.toHaveBeenCalled();
    const calls = vi.mocked(useWebSocketEvent).mock.calls;
    const disabledHandler = calls[calls.length - 1]?.[1];
    expect(disabledHandler).toBeDefined();
    disabledHandler?.({});
    expect(fetchMock).not.toHaveBeenCalled();

    rerender({ enabled: true });

    await waitFor(() => expect(result.current.allProjects).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("recovers from a pre-auth 401 once auth flips back to true", async () => {
    // Login flow: the optimistic mount fetch 401s, the auth status check
    // disables the hook, then a successful login re-enables it.
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 401 } as Response)
      .mockResolvedValue(okResponse([makeProject()]));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => {});

    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useProjects({ enabled }),
      { initialProps: { enabled: true } },
    );
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.allProjects).toHaveLength(0);

    rerender({ enabled: false });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    rerender({ enabled: true });

    await waitFor(() => expect(result.current.allProjects).toHaveLength(1));
    expect(result.current.error).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("searches checkout roots and tolerates projects without a checkout", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        okResponse([
          makeProject(),
          makeProject({
            id: "p2",
            name: "checkout-free",
            display_name: "Checkout Free",
            checkout: null,
          }),
        ]),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useProjects());

    await waitFor(() => expect(result.current.allProjects).toHaveLength(2));
    expect(result.current.allProjects[1]).not.toHaveProperty("repo_path");

    act(() => result.current.setSearchText("projects/gobby"));
    expect(result.current.projects.map((project) => project.id)).toEqual([
      "p1",
    ]);

    act(() => result.current.setSearchText("checkout free"));
    expect(result.current.projects.map((project) => project.id)).toEqual([
      "p2",
    ]);

    act(() => result.current.setSearchText("missing/root"));
    expect(result.current.projects).toEqual([]);
  });

  it("retries transient failures with bounded exponential backoff", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 500 } as Response)
      .mockResolvedValueOnce({ ok: false, status: 503 } as Response)
      .mockResolvedValueOnce(okResponse([makeProject()]));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => {});

    const { result } = renderHook(() => useProjects());
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.allProjects).toEqual([makeProject()]);
    expect(result.current.error).toBeNull();
  });

  it("cancels a pending retry when the hook unmounts", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
    } as Response);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = renderHook(() => useProjects());
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
