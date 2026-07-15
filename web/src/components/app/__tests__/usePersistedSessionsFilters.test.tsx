import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { usePersistedSessionsFilters } from "../usePersistedSessionsFilters";

function storedFilters(taskRefRole: "claimed" | "created"): string {
  return JSON.stringify({
    modes: [],
    providers: [],
    taskRefRoles: [taskRefRole],
    taskRefs: [],
    datePreset: "all",
    dateCustomFrom: null,
    dateCustomTo: null,
    branches: [],
    statuses: ["active", "paused"],
  });
}

describe("usePersistedSessionsFilters", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("loads and persists filters independently for each project", async () => {
    localStorage.setItem("gobby-sessions-filters:project-a", storedFilters("claimed"));
    localStorage.setItem("gobby-sessions-filters:project-b", storedFilters("created"));

    const { result, rerender } = renderHook(
      ({ projectId }) => usePersistedSessionsFilters(projectId),
      { initialProps: { projectId: "project-a" as string | null } },
    );

    expect([...result.current.sessionsFilters.taskRefRoles]).toEqual(["claimed"]);

    rerender({ projectId: "project-b" });
    await waitFor(() => {
      expect([...result.current.sessionsFilters.taskRefRoles]).toEqual(["created"]);
    });

    act(() => {
      result.current.setSessionsFilters((current) => ({
        ...current,
        taskRefMin: 42,
      }));
    });

    await waitFor(() => {
      expect(localStorage.getItem("gobby-sessions-filters:project-b")).toContain(
        '"taskRefMin":42',
      );
    });
    expect(localStorage.getItem("gobby-sessions-filters:project-a")).not.toContain(
      '"taskRefMin":42',
    );
  });
});
