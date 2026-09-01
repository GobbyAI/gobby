import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  useProjects,
  type ProjectWithStats,
} from "../../../../hooks/useProjects";
import { McpServerFields, type McpServerDraft } from "../McpServerFields";
import { saveMcpServerDraft } from "../McpTabActions";

vi.mock("../../../../hooks/useProjects", () => ({
  useProjects: vi.fn(),
}));

type ProjectsHookState = ReturnType<typeof useProjects> & { error?: unknown };

function makeProject(overrides: Partial<ProjectWithStats>): ProjectWithStats {
  return {
    id: "project-1",
    name: "alpha",
    display_name: "Alpha workspace",
    checkout: null,
    github_url: null,
    github_repo: null,
    linear_team_id: null,
    linear_project_id: null,
    approval_rules: [],
    validation_detection: null,
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    session_count: 0,
    open_task_count: 0,
    last_activity_at: null,
    ...overrides,
  };
}

function makeProjectsState(
  overrides: Partial<ProjectsHookState> = {},
): ProjectsHookState {
  const projects = [
    makeProject({
      id: "11111111-1111-4111-8111-111111111111",
      display_name: "Client portal",
      name: "client-portal",
    }),
    makeProject({
      id: "22222222-2222-4222-8222-222222222222",
      display_name: "",
      name: "ops-console",
    }),
  ];

  return {
    projects,
    allProjects: projects,
    isLoading: false,
    error: null,
    selectedProject: null,
    selectedProjectId: null,
    activeSubTab: "overview",
    setActiveSubTab: vi.fn(),
    searchText: "",
    setSearchText: vi.fn(),
    selectProject: vi.fn(),
    deselectProject: vi.fn(),
    updateProject: vi.fn(),
    deleteProject: vi.fn(),
    refresh: vi.fn(),
    totalSessions: 0,
    totalOpenTasks: 0,
    ...overrides,
  };
}

function makeDraft(overrides: Partial<McpServerDraft> = {}): McpServerDraft {
  return {
    name: "",
    description: "",
    transport: "http",
    url: "",
    command: "",
    args: [],
    env: {},
    headers: {},
    project_id: "11111111-1111-4111-8111-111111111111",
    enabled: true,
    requires_oauth: false,
    oauth_provider: "",
    connect_timeout: 30,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("McpServerFields", () => {
  it("saves a draft with project selection and key/value records", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn(async () => true);
    vi.mocked(useProjects).mockReturnValue(makeProjectsState());

    render(
      <McpServerFields mode="create" source={makeDraft()} onSave={onSave} />,
    );

    await user.type(
      screen.getByRole("textbox", { name: "Server name" }),
      "linear",
    );
    await user.type(
      screen.getByRole("textbox", { name: "URL" }),
      "https://mcp.linear.app",
    );
    await user.selectOptions(screen.getByLabelText("Project"), [
      "22222222-2222-4222-8222-222222222222",
    ]);

    const headers = screen.getByRole("group", { name: "Headers" });
    await user.click(within(headers).getByRole("button", { name: "Add row" }));
    await user.type(
      within(headers).getByRole("textbox", { name: "Key 1" }),
      "Authorization",
    );
    await user.type(
      within(headers).getByRole("textbox", { name: "Value 1" }),
      "Bearer token",
    );

    const env = screen.getByRole("group", { name: "Environment" });
    await user.click(within(env).getByRole("button", { name: "Add row" }));
    await user.type(
      within(env).getByRole("textbox", { name: "Key 1" }),
      "API_TOKEN",
    );
    await user.type(
      within(env).getByRole("textbox", { name: "Value 1" }),
      "secret",
    );

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith({
        ...makeDraft({
          name: "linear",
          url: "https://mcp.linear.app",
          project_id: "22222222-2222-4222-8222-222222222222",
          headers: { Authorization: "Bearer token" },
          env: { API_TOKEN: "secret" },
        }),
      });
    });
  });

  it("locks the registry key when editing an existing server", () => {
    vi.mocked(useProjects).mockReturnValue(makeProjectsState());

    render(
      <McpServerFields
        mode="edit"
        source={makeDraft({
          name: "github",
          transport: "stdio",
          command: "npx",
        })}
        onSave={vi.fn(async () => true)}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Server name" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Command" })).toHaveValue("npx");
  });
});

describe("saveMcpServerDraft", () => {
  it("posts create requests with env and headers intact", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ success: true })),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      saveMcpServerDraft({
        mode: "create",
        draft: makeDraft({
          name: "linear",
          url: "https://mcp.linear.app",
          headers: { Authorization: "Bearer token" },
          env: { API_TOKEN: "secret" },
        }),
      }),
    ).resolves.toBe(true);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/mcp/servers",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(
          makeDraft({
            name: "linear",
            url: "https://mcp.linear.app",
            headers: { Authorization: "Bearer token" },
            env: { API_TOKEN: "secret" },
          }),
        ),
      }),
    );
  });

  it("puts config updates before patching enabled state changes", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ success: true })),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      saveMcpServerDraft({
        mode: "edit",
        originalName: "github",
        originalEnabled: false,
        draft: makeDraft({
          name: "github",
          transport: "stdio",
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-github"],
          enabled: true,
        }),
      }),
    ).resolves.toBe(true);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/mcp/servers/github",
      expect.objectContaining({ method: "PUT" }),
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github"],
      enabled: false,
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/mcp/servers/github",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ enabled: true }),
      }),
    );
  });
});
