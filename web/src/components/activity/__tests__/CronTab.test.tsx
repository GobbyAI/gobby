import type { ReactElement, ReactNode } from "react";
import {
  fireEvent,
  render as baseRender,
  screen,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";

import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../ActivityActionsContext";
import { CronTab } from "../CronTab";
import {
  useCronJobs,
  type CronJob,
  type CronRun,
} from "../../../hooks/useCronJobs";

// The tab's toolbar (selector / Search) renders in the shared panel header in
// the real layout; mount it alongside the tab so those controls are reachable
// in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) => baseRender(ui, { wrapper: HeaderHarness });

const cronMock = vi.hoisted(() => ({
  jobs: [] as CronJob[],
  selectedJob: null as CronJob | null,
  selectJob: vi.fn(),
  runs: [] as CronRun[],
  isRunsLoading: false,
  isLoading: false,
  toggleJob: vi.fn(),
  deleteJob: vi.fn(),
  updateJob: vi.fn(),
}));

vi.mock("../../../hooks/useCronJobs", () => ({
  useCronJobs: vi.fn(() => cronMock),
}));

vi.mock("../../shared/ResizeHandle", () => ({
  ResizeHandle: () => null,
}));

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: "job-1",
    project_id: "p",
    name: "job-name",
    display_name: null,
    description: null,
    schedule_type: "cron",
    cron_expr: "0 3 * * *",
    interval_seconds: null,
    run_at: null,
    timezone: "UTC",
    action_type: "shell",
    action_config: {},
    enabled: true,
    is_system: false,
    next_run_at: "2099-01-01T00:00:00Z",
    last_run_at: null,
    last_status: null,
    consecutive_failures: 0,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  cronMock.jobs = [];
  cronMock.selectedJob = null;
  cronMock.selectJob = vi.fn();
  cronMock.runs = [];
  cronMock.isRunsLoading = false;
  cronMock.isLoading = false;
  cronMock.toggleJob = vi.fn();
  cronMock.deleteJob = vi.fn();
  cronMock.updateJob = vi.fn();
});

describe("CronTab", () => {
  it("passes the active project ID to the cron hook", () => {
    render(<CronTab projectId="project-123" />);

    expect(useCronJobs).toHaveBeenCalledWith("project-123");
  });

  it("renders an empty state when no jobs are loaded", () => {
    render(<CronTab projectId="p" />);
    expect(
      screen.getByText(/cron jobs appear here when scheduled/i),
    ).toBeInTheDocument();
  });

  it("renders rows for each job and calls selectJob on click", async () => {
    cronMock.jobs = [
      makeJob({ id: "a", name: "alpha" }),
      makeJob({ id: "b", name: "beta" }),
    ];
    render(<CronTab projectId="p" />);
    const beta = screen.getByRole("button", { name: "Select beta" });
    expect(beta).toBeInTheDocument();
    await userEvent.click(beta);
    expect(cronMock.selectJob).toHaveBeenCalledWith(
      expect.objectContaining({ id: "b" }),
    );
  });

  it("default-selects the topmost job so the runs pane is populated (#19152)", () => {
    cronMock.jobs = [
      makeJob({ id: "a", name: "alpha" }),
      makeJob({ id: "b", name: "beta" }),
    ];
    render(<CronTab projectId="p" />);
    expect(cronMock.selectJob).toHaveBeenCalledWith(
      expect.objectContaining({ id: "a" }),
    );
  });

  it("defaults to the Enabled filter with no All option (#19152)", async () => {
    cronMock.jobs = [
      makeJob({ id: "on", name: "live-job", enabled: true }),
      makeJob({ id: "off", name: "paused-job", enabled: false }),
    ];
    render(<CronTab projectId="p" />);
    expect(screen.queryByRole("radio", { name: "All" })).toBeNull();
    expect(screen.getByRole("radio", { name: "Enabled" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByText("live-job")).toBeInTheDocument();
    expect(screen.queryByText("paused-job")).toBeNull();

    await userEvent.click(screen.getByRole("radio", { name: "Disabled" }));
    expect(screen.queryByText("live-job")).toBeNull();
    expect(screen.getByText("paused-job")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "Enabled" }));
    expect(screen.getByText("live-job")).toBeInTheDocument();
    expect(screen.queryByText("paused-job")).toBeNull();
  });

  it("offers Rename, Disable, and Delete from the row menu (#19152)", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    cronMock.jobs = [makeJob({ id: "a", name: "alpha" })];
    render(<CronTab projectId="p" />);

    await userEvent.click(
      screen.getByRole("button", { name: "Open actions for alpha" }),
    );
    expect(
      screen.getByRole("menuitem", { name: "Rename" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "Disable" }));
    expect(cronMock.toggleJob).toHaveBeenCalledWith("a");

    await userEvent.click(
      screen.getByRole("button", { name: "Open actions for alpha" }),
    );
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    expect(window.confirm).toHaveBeenCalled();
    expect(cronMock.deleteJob).toHaveBeenCalledWith("a");
  });

  it("limits system cron menus to Rename (#19160)", async () => {
    cronMock.jobs = [
      makeJob({
        id: "sys",
        name: "gobby:wiki-prune",
        display_name: "Wiki prune",
        is_system: true,
      }),
    ];
    render(<CronTab projectId="p" />);

    await userEvent.click(
      screen.getByRole("button", { name: "Open actions for Wiki prune" }),
    );
    expect(
      screen.getByRole("menuitem", { name: "Rename" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Disable" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Delete" })).toBeNull();
  });

  it("renders display names in rows without the raw identifier (#19160)", () => {
    cronMock.jobs = [
      makeJob({
        id: "sys",
        name: "gobby:wiki-prune",
        display_name: "Wiki prune",
      }),
    ];
    render(<CronTab projectId="p" />);

    expect(screen.getByText("Wiki prune")).toBeInTheDocument();
    expect(screen.queryByText("gobby:wiki-prune")).toBeNull();
  });

  it("renames a job through the row menu (#19160)", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("Prune nightly");
    cronMock.jobs = [
      makeJob({
        id: "sys",
        name: "gobby:wiki-prune",
        display_name: "Wiki prune",
      }),
    ];
    render(<CronTab projectId="p" />);

    await userEvent.click(
      screen.getByRole("button", { name: "Open actions for Wiki prune" }),
    );
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));

    expect(cronMock.updateJob).toHaveBeenCalledWith("sys", {
      display_name: "Prune nightly",
    });
  });

  it("sends an empty display name to reset the label, skipping cancelled prompts (#19160)", async () => {
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("");
    cronMock.jobs = [
      makeJob({
        id: "sys",
        name: "gobby:wiki-prune",
        display_name: "Wiki prune",
      }),
    ];
    render(<CronTab projectId="p" />);

    await userEvent.click(
      screen.getByRole("button", { name: "Open actions for Wiki prune" }),
    );
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(cronMock.updateJob).toHaveBeenCalledWith("sys", {
      display_name: "",
    });

    cronMock.updateJob.mockClear();
    prompt.mockReturnValue(null);
    await userEvent.click(
      screen.getByRole("button", { name: "Open actions for Wiki prune" }),
    );
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(cronMock.updateJob).not.toHaveBeenCalled();
  });

  it("matches search queries against display names (#19160)", async () => {
    cronMock.jobs = [
      makeJob({
        id: "a",
        name: "gobby:wiki-prune",
        display_name: "Wiki prune",
      }),
      makeJob({ id: "b", name: "other-job" }),
    ];
    render(<CronTab projectId="p" />);

    await userEvent.click(
      screen.getByRole("button", { name: "Search cron jobs" }),
    );
    await userEvent.type(
      screen.getByRole("searchbox", { name: "Search cron jobs" }),
      "wiki pr",
    );

    expect(screen.getByText("Wiki prune")).toBeInTheDocument();
    expect(screen.queryByText("other-job")).toBeNull();
  });

  it("demotes the raw identifier to secondary text in the detail header (#19160)", () => {
    const job = makeJob({
      id: "sel",
      name: "gobby:wiki-prune",
      display_name: "Wiki prune",
      is_system: true,
    });
    cronMock.jobs = [job];
    cronMock.selectedJob = job;
    render(<CronTab projectId="p" />);

    const rawIdentifier = screen.getByTitle("gobby:wiki-prune");
    expect(rawIdentifier).toHaveTextContent("gobby:wiki-prune");
    expect(rawIdentifier.previousElementSibling).toHaveTextContent(
      "Wiki prune",
    );
  });

  it("labels the row menu toggle Enable for disabled jobs (#19152)", async () => {
    cronMock.jobs = [
      makeJob({ id: "off", name: "paused-job", enabled: false }),
    ];
    render(<CronTab projectId="p" />);
    await userEvent.click(screen.getByRole("radio", { name: "Disabled" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Open actions for paused-job" }),
    );
    await userEvent.click(screen.getByRole("menuitem", { name: "Enable" }));
    expect(cronMock.toggleJob).toHaveBeenCalledWith("off");
  });

  it("shows a Load more button when more jobs are available than the page size", () => {
    cronMock.jobs = Array.from({ length: 25 }, (_, i) =>
      makeJob({ id: `j${i}`, name: `job-${i}` }),
    );
    render(<CronTab projectId="p" />);
    expect(screen.getByText("Load more")).toBeInTheDocument();
    expect(screen.getByText("job-19")).toBeInTheDocument();
    expect(screen.queryByText("job-20")).toBeNull();

    fireEvent.click(screen.getByText("Load more"));
    expect(screen.getByText("job-20")).toBeInTheDocument();
    expect(screen.getByText("job-24")).toBeInTheDocument();
    expect(screen.queryByText("Load more")).toBeNull();
  });

  it("renders the runs list inside the detail pane when a job is selected", () => {
    const job = makeJob({ id: "sel", name: "selected-job" });
    cronMock.jobs = [job];
    cronMock.selectedJob = job;
    cronMock.runs = [
      {
        id: "r1",
        cron_job_id: "sel",
        triggered_at: "2026-05-01T12:00:00Z",
        started_at: null,
        completed_at: null,
        status: "success",
        output: null,
        error: null,
        agent_run_id: null,
        pipeline_execution_id: null,
        child: null,
        created_at: "2026-05-01T12:00:00Z",
      },
    ];
    render(<CronTab projectId="p" />);
    expect(screen.getByText(/success/i)).toBeInTheDocument();
  });

  it("shows dispatched child status in the runs list", () => {
    const job = makeJob({ id: "sel", name: "selected-job" });
    cronMock.jobs = [job];
    cronMock.selectedJob = job;
    cronMock.runs = [
      {
        id: "r1",
        cron_job_id: "sel",
        triggered_at: "2026-05-01T12:00:00Z",
        started_at: "2026-05-01T12:00:01Z",
        completed_at: "2026-05-01T12:00:02Z",
        status: "dispatched",
        output: "Pipeline dispatched",
        error: null,
        agent_run_id: null,
        pipeline_execution_id: "pipe-1",
        child: {
          type: "pipeline_execution",
          id: "pipe-1",
          status: "waiting_approval",
          terminal: false,
          missing: false,
        },
        created_at: "2026-05-01T12:00:00Z",
      },
    ];
    render(<CronTab projectId="p" />);
    expect(screen.getByText("dispatched")).toBeInTheDocument();
    expect(screen.getByText("pipeline waiting approval")).toBeInTheDocument();
  });

  it("shows an interrupted run as a warning rather than a failure", () => {
    const job = makeJob({ id: "sel", name: "selected-job" });
    cronMock.jobs = [job];
    cronMock.selectedJob = job;
    cronMock.runs = [
      {
        id: "r1",
        cron_job_id: "sel",
        triggered_at: "2026-05-01T12:00:00Z",
        started_at: "2026-05-01T12:00:01Z",
        completed_at: "2026-05-01T13:30:00Z",
        status: "interrupted",
        output: null,
        error: "Cron run was interrupted by a daemon restart",
        agent_run_id: null,
        pipeline_execution_id: null,
        child: null,
        created_at: "2026-05-01T12:00:00Z",
      },
    ];
    render(<CronTab projectId="p" />);
    expect(screen.getByText("interrupted")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Interrupted" })).toHaveAttribute(
      "data-kind",
      "warning",
    );
    expect(screen.queryByRole("img", { name: "Failure" })).toBeNull();
  });
});
