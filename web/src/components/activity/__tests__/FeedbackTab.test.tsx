import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FeedbackTab } from "../FeedbackTab";
import type {
  FeedbackRun,
  FeedbackSchedule,
  FeedbackStatus,
} from "../../../hooks/useFeedbackReview";

function makeRun(overrides: Partial<FeedbackRun> = {}): FeedbackRun {
  return {
    id: "run-1",
    status: "completed",
    dry_run: false,
    rows_considered: 4,
    actions: { filed: [], deduplicated: 0 },
    digest_md: "## Hook friction\n\nTwo sessions hit the same guard.",
    error: null,
    created_at: "2026-08-02T03:00:00+00:00",
    ...overrides,
  };
}

const SCHEDULE: FeedbackSchedule = {
  enabled: true,
  cron_expr: "0 3 * * *",
  timezone: "America/Chicago",
  next_run_at: "2026-08-03T08:00:00+00:00",
  last_status: "completed",
};

const STATUS: FeedbackStatus = { backlog: 7, schedule: SCHEDULE };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

function serve(status: FeedbackStatus, runs: FeedbackRun[]): void {
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/feedback/status")
      return json({ success: true, ...status });
    if (url === "/api/feedback/runs?limit=20")
      return json({ success: true, runs });
    return json({ detail: "unexpected" }, 404);
  });
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("FeedbackTab", () => {
  it("shows a loading state before the first response", () => {
    fetchMock.mockReturnValue(new Promise(() => {}));

    render(<FeedbackTab />);

    expect(screen.getByText("Loading feedback review…")).toBeInTheDocument();
  });

  it("summarizes backlog and schedule and lists runs in API order", async () => {
    serve(STATUS, [
      makeRun({ id: "run-2", rows_considered: 9 }),
      makeRun({ id: "run-1", status: "failed", error: "provider unavailable" }),
    ]);

    render(<FeedbackTab />);

    const summary = await screen.findByTestId("feedback-status-strip");
    expect(summary).toHaveTextContent("7 unreviewed");
    expect(summary).toHaveTextContent("0 3 * * * (America/Chicago)");
    expect(summary).not.toHaveTextContent("paused");
    const rows = screen.getAllByTestId("feedback-run-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("9 rows");
    expect(rows[1]).toHaveTextContent("Failed");
  });

  it("uses the singular for a run that considered one row", async () => {
    const user = userEvent.setup();
    serve(STATUS, [makeRun({ rows_considered: 1 })]);
    render(<FeedbackTab />);

    const row = await screen.findByTestId("feedback-run-row");
    expect(row).toHaveTextContent("1 row");
    expect(row).not.toHaveTextContent("1 rows");
    await user.click(row);
    const detail = screen.getByTestId("feedback-run-detail");
    expect(detail).toHaveTextContent("1 row,");
    expect(detail).not.toHaveTextContent("1 rows");
  });

  it("labels a disabled or missing schedule", async () => {
    serve({ ...STATUS, schedule: { ...SCHEDULE, enabled: false } }, []);
    const { unmount } = render(<FeedbackTab />);
    expect(
      await screen.findByTestId("feedback-status-strip"),
    ).toHaveTextContent("paused");
    unmount();

    serve({ ...STATUS, schedule: null }, []);
    render(<FeedbackTab />);
    expect(
      await screen.findByTestId("feedback-status-strip"),
    ).toHaveTextContent("not scheduled");
  });

  it("renders the empty state when no review has run", async () => {
    serve({ ...STATUS, backlog: 0 }, []);

    render(<FeedbackTab />);

    expect(
      await screen.findByText(
        "Review runs appear here after the nightly feedback review",
      ),
    ).toBeInTheDocument();
  });

  it("opens a run's digest and filed task refs", async () => {
    const user = userEvent.setup();
    serve(STATUS, [
      makeRun({
        id: "run-7",
        dry_run: true,
        actions: {
          filed: [{ task_ref: "#23401", title: "Quiet the read guard" }],
          deduplicated: 2,
        },
      }),
    ]);
    render(<FeedbackTab />);

    await user.click(await screen.findByTestId("feedback-run-row"));

    const detail = screen.getByTestId("feedback-run-detail");
    expect(within(detail).getByText("Dry run")).toBeInTheDocument();
    expect(
      within(detail).getByRole("heading", { name: "Hook friction" }),
    ).toBeInTheDocument();
    const filed = within(detail).getByRole("list", { name: "Filed tasks" });
    expect(filed).toHaveTextContent("#23401");
    expect(filed).toHaveTextContent("Quiet the read guard");
    expect(detail).toHaveTextContent("2 deduplicated");
  });

  it("shows a failed run's error and a missing digest", async () => {
    const user = userEvent.setup();
    serve(STATUS, [
      makeRun({
        status: "failed",
        error: "provider unavailable",
        digest_md: null,
      }),
    ]);
    render(<FeedbackTab />);

    await user.click(await screen.findByTestId("feedback-run-row"));

    const detail = screen.getByTestId("feedback-run-detail");
    expect(within(detail).getByRole("alert")).toHaveTextContent(
      "provider unavailable",
    );
    expect(detail).toHaveTextContent("No digest recorded");
    expect(detail).toHaveTextContent("No tasks filed");
  });

  it("surfaces the daemon's error detail when the service is unavailable", async () => {
    fetchMock.mockImplementation(async () =>
      json({ detail: "feedback review service is unavailable" }, 503),
    );

    render(<FeedbackTab />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "feedback review service is unavailable",
    );
  });
});
