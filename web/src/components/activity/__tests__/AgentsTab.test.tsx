import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ACTIVITY_PANEL_TABS } from "../ActivityPanelTabs";
import { AgentsTab } from "../AgentsTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";

vi.mock("../../../lib/providerModels", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../lib/providerModels")>();
  return {
    ...actual,
    fetchProviderModelCatalog: vi.fn(async () => [
      {
        provider: "codex",
        available: true,
        source: "static",
        models: [{ value: "gpt-5", label: "GPT-5" }],
      },
      {
        provider: "claude",
        available: true,
        source: "static",
        models: [{ value: "opus", label: "Opus" }],
      },
    ]),
  };
});

vi.mock("../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

function agentDefinition(overrides: Record<string, unknown> = {}) {
  return {
    definition: {
      name: "reviewer",
      description: "Reviews pull requests",
      surfaces: ["spawn"],
      prompts: { agent: "Review the assigned implementation." },
      provider: "claude",
      model: "opus",
      reasoning_effort: null,
      reasoning_required: false,
      fallback_agent: null,
      mode: "inherit",
      isolation: "worktree",
      base_branch: "inherit",
      timeout: 0,
      workflows: {
        variables: { EXISTING: "value" },
      },
      lifecycle_variables: {},
      default_variables: {},
      blocked_tools: [],
      blocked_mcp_tools: [],
      ...overrides,
    },
    source: "installed",
    source_path: null,
    db_id: "agent-db-1",
    enabled: true,
    overridden_by: null,
    deleted_at: null,
    tags: ["review"],
  };
}

function installFetch(definitions = [agentDefinition()]): MockFetchInstance {
  const mockFetch = createMockFetch();
  mockFetch.mockJsonResponse("/api/agents/definitions", {
    status: "success",
    definitions,
  });
  mockFetch.mockJsonResponse("/api/pipelines/definitions", {
    definitions: [{ id: "pipe-1", name: "nightly-verify" }],
  });
  mockFetch.mockJsonResponse("/api/rules/tags", { tags: [] });
  mockFetch.mockJsonResponse("/api/rules/groups", { groups: [] });
  mockFetch.mockJsonResponse("/api/rules", { rules: [] });
  mockFetch.mockJsonResponse("/api/skills", { skills: [] });
  return mockFetch;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("AgentsTab", () => {
  it("is registered in the activity tab selector", () => {
    expect(ACTIVITY_PANEL_TABS.map((tab) => tab.id)).toContain("agents");
  });

  it("saves scalar fields, tags, and embedded variable draft state", async () => {
    const user = userEvent.setup();
    const mockFetch = installFetch();

    render(<AgentsTab projectId="project-1" />);

    await user.click(
      await screen.findByRole("button", { name: "Select reviewer" }),
    );
    const pipelineSelect = screen.getByLabelText("Pipeline");
    expect(pipelineSelect).toBeInTheDocument();
    expect(
      within(pipelineSelect).getByRole("option", { name: "nightly-verify" }),
    ).toBeInTheDocument();
    const pipelineCall = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .find((url) => url.includes("/api/pipelines/definitions"));
    expect(pipelineCall).toBeDefined();
    expect(pipelineCall).not.toContain(["workflow", "type"].join("_"));
    await user.clear(screen.getByRole("textbox", { name: "Description" }));
    await user.type(
      screen.getByRole("textbox", { name: "Description" }),
      "Updated reviewer",
    );
    await user.selectOptions(screen.getByLabelText("Provider"), "codex");
    await user.clear(screen.getByRole("textbox", { name: "Model" }));
    await user.type(screen.getByRole("textbox", { name: "Model" }), "gpt-5");

    await user.type(screen.getByLabelText("Add Tags"), "qa{enter}");
    await user.click(screen.getByRole("button", { name: "+ Add Variable" }));
    await user.type(screen.getByPlaceholderText("Key"), "API_TOKEN");
    await user.type(screen.getByPlaceholderText("Value"), "secret");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const putCall = mockFetch.fn.mock.calls.find(
        ([url, init]) =>
          String(url) === "/api/agents/definitions/agent-db-1" &&
          (init as RequestInit | undefined)?.method === "PUT",
      );
      expect(putCall).toBeTruthy();
      const body = JSON.parse(String((putCall?.[1] as RequestInit).body));
      expect(body).toMatchObject({
        name: "reviewer",
        description: "Updated reviewer",
        provider: "codex",
        model: "gpt-5",
        tags: ["review", "qa"],
        workflows: {
          variables: {
            EXISTING: "value",
            API_TOKEN: "secret",
          },
        },
      });
    });

    mockFetch.restore();
  });

  it("runs the row action for disabling agents", async () => {
    const user = userEvent.setup();
    const mockFetch = installFetch();

    render(<AgentsTab projectId="project-1" />);

    const row = await screen.findByRole("listitem", { name: /reviewer/i });
    await user.click(
      within(row).getByRole("button", { name: "Open actions for reviewer" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Disable" }));

    await waitFor(() => {
      expect(mockFetch.fn).toHaveBeenCalledWith(
        "/api/agents/definitions/agent-db-1",
        expect.objectContaining({ method: "PUT" }),
      );
    });

    mockFetch.restore();
  });

  it("renders list and detail metadata through shared chips", async () => {
    const user = userEvent.setup();
    const mockFetch = installFetch();

    render(<AgentsTab projectId="project-1" />);

    const row = await screen.findByRole("listitem", { name: /reviewer/i });
    expect(within(row).getByText("claude")).toHaveClass("inline-flex");
    expect(within(row).getByText("Installed")).toHaveClass("inline-flex");

    await user.click(
      within(row).getByRole("button", { name: "Select reviewer" }),
    );
    expect(await screen.findByText("installed")).toHaveClass("inline-flex");

    mockFetch.restore();
  });

  it("runs the row action for duplicating agents", async () => {
    const user = userEvent.setup();
    const mockFetch = installFetch();

    render(<AgentsTab projectId="project-1" />);

    const row = await screen.findByRole("listitem", { name: /reviewer/i });
    await user.click(
      within(row).getByRole("button", { name: "Open actions for reviewer" }),
    );
    await user.click(
      await screen.findByRole("menuitem", { name: "Duplicate" }),
    );

    await waitFor(() => {
      expect(mockFetch.fn).toHaveBeenCalledWith(
        "/api/agents/definitions",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"name":"reviewer-copy"'),
        }),
      );
    });

    mockFetch.restore();
  });
});
