import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createMockFetch,
  type MockFetchInstance,
} from "../../test/mocks/fetch";
import { useCronJobs, type CreateCronJobRequest } from "../useCronJobs";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

let mockFetch: MockFetchInstance;

beforeEach(() => {
  mockFetch = createMockFetch();
  mockFetch.mockJsonResponse(/\/api\/cron\/jobs\?project_id=project-123$/, {
    jobs: [],
  });
  mockFetch.mockJsonResponse(/\/api\/cron\/jobs$/, {
    job: { id: "job-1", project_id: "project-123", name: "Daily cleanup" },
  });
});

afterEach(() => {
  mockFetch.restore();
  vi.restoreAllMocks();
});

describe("useCronJobs", () => {
  it("includes the active project ID in the create payload", async () => {
    const { result } = renderHook(() => useCronJobs("project-123"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const formValues: Omit<CreateCronJobRequest, "project_id"> = {
      name: "Daily cleanup",
      action_type: "shell",
      action_config: { command: "gobby cleanup" },
      cron_expr: "0 3 * * *",
    };

    await act(async () => {
      await result.current.createJob(formValues);
    });

    const createCall = mockFetch.fn.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    const payload = JSON.parse(
      String(createCall?.[1]?.body),
    ) as CreateCronJobRequest;

    expect(payload).toEqual({ ...formValues, project_id: "project-123" });
    expect(result.current.jobs).toContainEqual(
      expect.objectContaining({ id: "job-1", project_id: "project-123" }),
    );

    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(/\/api\/cron\/jobs\?project_id=project-123$/, {
      jobs: [{ id: "job-1", project_id: "project-123", name: "Daily cleanup" }],
    });

    act(() => result.current.refresh());

    await waitFor(() => {
      expect(result.current.jobs).toContainEqual(
        expect.objectContaining({ id: "job-1", project_id: "project-123" }),
      );
    });
    expect(String(mockFetch.fn.mock.calls[0]?.[0])).toContain(
      "project_id=project-123",
    );
  });

  it("does not create an unscoped job", async () => {
    const errorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const { result } = renderHook(() => useCronJobs(null));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    let createdJob:
      Awaited<ReturnType<typeof result.current.createJob>> | undefined;
    await act(async () => {
      createdJob = await result.current.createJob({
        name: "Daily cleanup",
        action_type: "shell",
        action_config: { command: "gobby cleanup" },
      });
    });

    expect(createdJob).toBeNull();
    expect(
      mockFetch.fn.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
    expect(errorSpy).toHaveBeenCalledWith(
      "Cannot create cron job without a project ID",
    );
  });
});
