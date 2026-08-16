import {
  fireEvent,
  render as baseRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../../ActivityActionsContext";
import { PipelinesTab } from "../../PipelinesTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../../test/mocks/fetch";

// The tab's segment selector renders in the shared panel header in the real
// layout; mount it alongside the tab so the control is reachable in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) => baseRender(ui, { wrapper: HeaderHarness });

vi.mock("../../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

vi.mock("../../../shared/executions/execution-utils", () => ({
  PipelineStatusDot: ({ status }: { status: string }) => <span>{status}</span>,
  StepDisplay: () => null,
}));

vi.mock("../../../shared/executions/executionFormatters", () => ({
  formatDateTime: (value: string) => value,
  formatDuration: () => "1m",
}));

let mockFetch: MockFetchInstance;

describe("Pipelines defs segment", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    window.localStorage.removeItem("gobby-pipelines-segment-v1");
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse(/\/api\/pipelines\/executions\?/, {
      executions: [
        {
          id: "exec-1",
          pipeline_name: "Nightly sync",
          status: "running",
          created_at: "2026-04-09T00:00:00Z",
        },
      ],
    });
    mockFetch.mockJsonResponse("/api/pipelines/exec-1", {
      execution: {
        id: "exec-1",
        pipeline_name: "Nightly sync",
        status: "running",
        created_at: "2026-04-09T00:00:00Z",
        steps: [],
      },
    });
    mockFetch.mockJsonResponse(/\/api\/pipelines\/definitions/, {
      definitions: [
        {
          id: "wf-1",
          name: "deploy-prod",
          kind: "pipeline",
          description: "Deploy production services with staged approvals.",
          definition_json: JSON.stringify({ name: "deploy-prod", steps: [] }),
          enabled: true,
          source: "installed",
          version: "1.0",
          tags: ["release"],
        },
      ],
    });
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    mockFetch.restore();
    vi.restoreAllMocks();
    window.localStorage.removeItem("gobby-pipelines-segment-v1");
  });

  it("defaults to Live, switches to Defs, and persists the selected segment", async () => {
    render(<PipelinesTab projectId="project-1" />);

    await waitFor(() => {
      expect(screen.getByText("Nightly sync")).toBeInTheDocument();
    });
    expect(screen.getByRole("radio", { name: "Live" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    fireEvent.click(screen.getByRole("radio", { name: "Defs" }));

    await waitFor(() => {
      expect(
        within(
          screen.getByRole("list", { name: "Pipeline definitions" }),
        ).getByText("deploy-prod"),
      ).toBeInTheDocument();
    });
    // Single-line rows surface the definition name + chips; the description now
    // lives in the detail pane, not inline on the list row.
    expect(
      within(
        screen.getByRole("list", { name: "Pipeline definitions" }),
      ).queryByText("Deploy production services with staged approvals."),
    ).not.toBeInTheDocument();
    const pipelineChips = screen.getAllByText("PIPELINE");
    expect(pipelineChips).toHaveLength(2);
    expect(pipelineChips.every((chip) => chip.classList.contains("h-5"))).toBe(
      true,
    );
    expect(screen.getByRole("radio", { name: "Defs" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(window.localStorage.getItem("gobby-pipelines-segment-v1")).toBe(
      "defs",
    );

    const pipelineCall = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .find((url) => url.includes("/api/pipelines/definitions"));

    expect(pipelineCall).toBeDefined();
    expect(pipelineCall).not.toContain(["workflow", "type"].join("_"));
    expect(pipelineCall).toContain("include_deleted=true");
    expect(pipelineCall).toContain("project_id=project-1");
  });

  it("adds and saves a pipeline editor step", async () => {
    mockFetch.mockJsonResponse("/api/pipelines/definitions/wf-1", {
      definition: {
        id: "wf-1",
        name: "deploy-prod",
        kind: "pipeline",
        description: "Deploy production services with staged approvals.",
        definition_json: JSON.stringify({
          name: "deploy-prod",
          description: "Deploy production services with staged approvals.",
          steps: [{ id: "step-1", exec: "npm test -- --runInBand" }],
        }),
        enabled: true,
        source: "installed",
        version: "1.0",
        tags: ["release"],
      },
    });
    render(<PipelinesTab projectId="project-1" />);

    fireEvent.click(screen.getByRole("radio", { name: "Defs" }));

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "+ Add Step" }));
    fireEvent.click(screen.getByRole("button", { name: "Exec" }));
    fireEvent.change(screen.getByPlaceholderText("shell command"), {
      target: { value: "npm test -- --runInBand" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const saveCall = mockFetch.fn.mock.calls.find(
        ([url, init]) =>
          String(url).includes("/api/pipelines/definitions/wf-1") &&
          (init as RequestInit | undefined)?.method === "PUT",
      );
      expect(saveCall).toBeDefined();
    });

    const saveCall = mockFetch.fn.mock.calls.find(
      ([url, init]) =>
        String(url).includes("/api/pipelines/definitions/wf-1") &&
        (init as RequestInit | undefined)?.method === "PUT",
    );
    const requestBody = JSON.parse(
      (saveCall?.[1] as RequestInit).body as string,
    );
    const definition = JSON.parse(requestBody.definition_json);

    expect(requestBody.name).toBe("deploy-prod");
    expect(requestBody.description).toBe(
      "Deploy production services with staged approvals.",
    );
    expect(definition.steps).toEqual([
      { id: "step-1", exec: "npm test -- --runInBand" },
    ]);
  });

  it("switches from definition detail to the pipeline editor and back", async () => {
    render(<PipelinesTab projectId="project-1" />);

    fireEvent.click(screen.getByRole("radio", { name: "Defs" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));

    expect(screen.getByPlaceholderText("Pipeline name")).toHaveValue(
      "deploy-prod",
    );

    fireEvent.click(screen.getByRole("button", { name: "←" }));

    expect(
      await screen.findByRole("button", { name: "Edit" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByPlaceholderText("Pipeline name"),
    ).not.toBeInTheDocument();
  });
});
