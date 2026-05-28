import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityMcpTab, type ActivityMcpTabProps } from "../ActivityMcpTab";

const servers = [
  {
    name: "gobby-tasks",
    state: "connected",
    connected: true,
    available: true,
    transport: "internal",
  },
  {
    name: "github",
    state: "pending",
    connected: false,
    available: true,
    transport: "stdio",
  },
];

const toolsByServer = {
  "gobby-tasks": [
    { name: "list_tasks", brief: "List project tasks" },
    { name: "create_task", brief: "Create a task" },
  ],
  github: [
    { name: "create_issue", brief: "Open a GitHub issue" },
  ],
};

const status = {
  total_servers: 2,
  connected_servers: 1,
  cached_tools: 3,
  server_health: {
    "gobby-tasks": { state: "connected", health: "healthy", failures: 0 },
    github: { state: "pending", health: "degraded", failures: 1 },
  },
};

function makeProps(
  overrides: Partial<ActivityMcpTabProps> = {},
): ActivityMcpTabProps {
  return {
    servers,
    toolsByServer,
    status,
    isLoading: false,
    searchText: "",
    setSearchText: vi.fn(),
    addServer: vi.fn(async () => true),
    removeServer: vi.fn(async () => true),
    refreshToolCache: vi.fn(async () => true),
    fetchToolSchema: vi.fn(async (_serverName, toolName) => ({
      name: toolName,
      description: `${toolName} schema`,
      inputSchema: {
        type: "object",
        properties: {
          title: { type: "string", description: "Task title" },
        },
        required: ["title"],
      },
    })),
    callTool: vi.fn(async () => ({
      success: true,
      result: { ok: true },
    })),
    ...overrides,
  };
}

function renderMcp(props = makeProps()) {
  function Harness() {
    const [searchText, setSearchText] = useState(props.searchText);
    return (
      <ActivityMcpTab
        {...props}
        searchText={searchText}
        setSearchText={setSearchText}
      />
    );
  }

  return render(<Harness />);
}

function treeItemFor(text: string) {
  const tree = screen.getByRole("tree", { name: "MCP servers and tools" });
  return within(tree)
    .getAllByText(text)[0]
    .closest('[role="treeitem"]') as HTMLElement;
}

describe("ActivityMcpTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders a two-level server/tool tree and supports expansion", async () => {
    const user = userEvent.setup();
    renderMcp();

    const serverRow = await screen.findByText("gobby-tasks");
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("list_tasks")).toBeNull();

    await user.click(serverRow);

    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("list_tasks")).toBeInTheDocument();
  });

  it("searches server names, tool names, and tool briefs", async () => {
    const user = userEvent.setup();
    renderMcp();

    await user.type(screen.getByPlaceholderText("Search MCP"), "issue");

    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.getByText("create_issue")).toBeInTheDocument();
    expect(screen.queryByText("gobby-tasks")).toBeNull();
  });

  it("filters Internal and External servers from the toolbar dropdown", async () => {
    const user = userEvent.setup();
    renderMcp();

    await user.click(screen.getByRole("button", { name: "Filter MCP server types" }));

    const menu = screen.getByRole("menu");
    expect(within(menu).getByRole("menuitemcheckbox", { name: /Internal/ }))
      .toHaveAttribute("aria-checked", "true");
    expect(within(menu).getByRole("menuitemcheckbox", { name: /External/ }))
      .toHaveAttribute("aria-checked", "true");

    await user.click(within(menu).getByRole("menuitemcheckbox", { name: /External/ }));

    expect(screen.getByText("gobby-tasks")).toBeInTheDocument();
    expect(screen.queryByText("github")).toBeNull();
  });

  it("opens the add-server modal from the toolbar", async () => {
    const user = userEvent.setup();
    renderMcp();

    await user.click(screen.getByRole("button", { name: "Add MCP server" }));

    expect(screen.getByRole("heading", { name: "Add MCP Server" })).toBeInTheDocument();
  });

  it("removes external servers through the server menu", async () => {
    const user = userEvent.setup();
    const removeServer = vi.fn(async () => true);
    renderMcp(makeProps({ removeServer }));

    await user.click(screen.getByRole("button", { name: "Open actions for github server" }));
    await user.click(screen.getByRole("menuitem", { name: "Remove server..." }));
    await user.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => {
      expect(removeServer).toHaveBeenCalledWith("github");
    });
  });

  it("loads and displays tool schema details", async () => {
    const user = userEvent.setup();
    const fetchToolSchema = vi.fn(makeProps().fetchToolSchema);
    renderMcp(makeProps({ fetchToolSchema }));

    await user.click(await screen.findByText("gobby-tasks"));
    await user.click(await screen.findByText("list_tasks"));

    await waitFor(() => {
      expect(fetchToolSchema).toHaveBeenCalledWith("gobby-tasks", "list_tasks");
    });
    expect(screen.getByText("Input Schema")).toBeInTheDocument();
    expect(screen.getByText(/list_tasks schema/)).toBeInTheDocument();
    expect(screen.getByText(/"title"/)).toBeInTheDocument();
  });

  it("executes a tool from the tool menu and renders result JSON", async () => {
    const user = userEvent.setup();
    const callTool = vi.fn(async () => ({
      success: true,
      result: { ok: true },
    }));
    renderMcp(makeProps({ callTool }));

    await user.click(await screen.findByText("gobby-tasks"));
    await user.click(
      screen.getByRole("button", { name: "Open actions for gobby-tasks.create_task" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Execute tool..." }));
    await screen.findByText("create_task schema");

    await user.type(screen.getByRole("textbox", { name: /title/ }), "Fix docs");
    await user.click(screen.getByRole("button", { name: "Execute" }));

    await waitFor(() => {
      expect(callTool).toHaveBeenCalledWith(
        "gobby-tasks",
        "create_task",
        { title: "Fix docs" },
      );
    });
    expect(screen.getByLabelText("Tool result JSON")).toHaveTextContent('"ok": true');
  });

  it("refreshes MCP tools from the toolbar", async () => {
    const user = userEvent.setup();
    const refreshToolCache = vi.fn(async () => true);
    renderMcp(makeProps({ refreshToolCache }));

    await user.click(screen.getByRole("button", { name: "Refresh MCP tools" }));

    expect(refreshToolCache).toHaveBeenCalledOnce();
  });
});
