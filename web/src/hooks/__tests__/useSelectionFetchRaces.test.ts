import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useCronJobs, type CronJob } from "../useCronJobs";
import { usePipelineDefs } from "../usePipelineDefs";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: () => undefined,
}));

interface DeferredResponse {
  promise: Promise<Response>;
  resolve: (response: Response) => void;
  reject: (error: Error) => void;
}

function deferredResponse(): DeferredResponse {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

function cronJob(id: string, projectId: string): CronJob {
  return {
    id,
    project_id: projectId,
    name: id,
    display_name: null,
    description: null,
    schedule_type: "cron",
    cron_expr: "* * * * *",
    interval_seconds: null,
    run_at: null,
    timezone: "UTC",
    action_type: "pipeline",
    action_config: {},
    enabled: true,
    is_system: false,
    next_run_at: null,
    last_run_at: null,
    last_status: null,
    consecutive_failures: 0,
    created_at: "",
    updated_at: "",
  };
}

describe("selection fetch race protection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the latest cron project list and run selection", async () => {
    const projectA = deferredResponse();
    const projectB = deferredResponse();
    const runsA = deferredResponse();
    const runsB = deferredResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/runs"))
          return url.includes("/job-a/") ? runsA.promise : runsB.promise;
        return url.includes("project_id=project-a")
          ? projectA.promise
          : projectB.promise;
      }),
    );

    const { result, rerender } = renderHook(
      ({ projectId }) => useCronJobs(projectId),
      { initialProps: { projectId: "project-a" } },
    );
    await act(async () => {
      projectA.resolve(jsonResponse({ jobs: [cronJob("job-a", "project-a")] }));
    });
    act(() => result.current.selectJob(cronJob("job-a", "project-a")));
    rerender({ projectId: "project-b" });

    await act(async () => {
      projectB.resolve(jsonResponse({ jobs: [cronJob("job-b", "project-b")] }));
    });
    expect(result.current.jobs.map((job) => job.id)).toEqual(["job-b"]);
    expect(result.current.selectedJob).toBeNull();

    act(() => result.current.selectJob(cronJob("job-b", "project-b")));
    await act(async () => {
      runsB.resolve(jsonResponse({ runs: [{ id: "run-b" }] }));
    });
    await act(async () => {
      runsA.reject(new Error("stale run failure"));
    });

    expect(result.current.runs.map((run) => run.id)).toEqual(["run-b"]);
    expect(result.current.isRunsLoading).toBe(false);
  });

  it("rejects cron mutations for jobs outside the active project", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ jobs: [cronJob("job-a", "project-a")] }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCronJobs("project-a"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await expect(result.current.runNow("job-b")).resolves.toBeNull();
    await expect(result.current.deleteJob("job-b")).resolves.toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the latest pipeline filter results and detail selection", async () => {
    const initialList = deferredResponse();
    const filteredA = deferredResponse();
    const filteredB = deferredResponse();
    const detailA = deferredResponse();
    const detailB = deferredResponse();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/pipelines/definitions"))
          return initialList.promise;
        if (url.includes("project_id=project-a")) return filteredA.promise;
        if (url.includes("project_id=project-b")) return filteredB.promise;
        if (url.endsWith("/pipeline-a")) return detailA.promise;
        return detailB.promise;
      }),
    );

    const { result } = renderHook(() => usePipelineDefs());
    await act(async () => {
      initialList.resolve(jsonResponse({ definitions: [] }));
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    act(() => {
      void result.current.fetchPipelines({ project_id: "project-a" });
      void result.current.fetchPipelines({ project_id: "project-b" });
    });
    await act(async () => {
      filteredB.resolve(jsonResponse({ definitions: [{ id: "pipeline-b" }] }));
    });
    await act(async () => {
      filteredA.reject(new Error("stale list failure"));
    });
    expect(result.current.pipelines.map((pipeline) => pipeline.id)).toEqual([
      "pipeline-b",
    ]);

    act(() => {
      void result.current.selectPipeline("pipeline-a");
      void result.current.selectPipeline("pipeline-b");
    });
    await act(async () => {
      detailB.resolve(jsonResponse({ definition: { id: "pipeline-b" } }));
    });
    await act(async () => {
      detailA.resolve(jsonResponse({ definition: { id: "pipeline-a" } }));
    });

    expect(result.current.selectedId).toBe("pipeline-b");
    expect(result.current.selectedPipeline?.id).toBe("pipeline-b");
  });
});
