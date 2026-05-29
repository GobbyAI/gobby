import { useState, type ReactNode } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityMcpTab, type ActivityMcpTabProps } from "../ActivityMcpTab";
import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../ActivityActionsContext";

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
  github: [{ name: "create_issue", brief: "Open a GitHub issue" }],
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
    setServerEnabled: vi.fn(async () => true),
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

function Harness({
  props,
  children,
}: {
  props: ActivityMcpTabProps;
  children?: ReactNode;
}) {
  const [searchText, setSearchText] = useState(props.searchText);
  return (
    <ActivityActionsProvider>
      {children}
      <ActivityMcpTab
        {...props}
        searchText={searchText}
        setSearchText={setSearchText}
      />
    </ActivityActionsProvider>
  );
}

function renderMcp(props = makeProps()) {
  // The shared header buttons live above the tab in the real layout; render
  // them here so Add/Refresh are reachable in tests.
  return render(<Harness props={props}>{<ActivityActionButtons />}</Harness>);
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

  it("filters by server type with the All | Internal | External selector", async () => {
    const user = userEvent.setup();
    renderMcp();

    const group = screen.getByRole("radiogroup", { name: "MCP server type" });
    expect(within(group).getByRole("radio", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.click(within(group).getByRole("radio", { name: "Internal" }));
    expect(screen.getByText("gobby-tasks")).toBeInTheDocument();
    expect(screen.queryByText("github")).toBeNull();

    await user.click(within(group).getByRole("radio", { name: "External" }));
    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.queryByText("gobby-tasks")).toBeNull();
  });

  it("opens the add-server modal from the shared header", async () => {
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

  it("toggles an external server's enabled state from the menu", async () => {
    const user = userEvent.setup();
    const setServerEnabled = vi.fn(async () => true);
    renderMcp(makeProps({ setServerEnabled }));

    await user.click(screen.getByRole("button", { name: "Open actions for github server" }));
    await user.click(screen.getByRole("menuitem", { name: "Disable server" }));

    await waitFor(() => {
      expect(setServerEnabled).toHaveBeenCalledWith("github", false);
    });
  });

  it("does not offer enable/disable or remove for internal servers", async () => {
    const user = userEvent.setup();
    renderMcp();

    await user.click(
      screen.getByRole("button", { name: "Open actions for gobby-tasks server" }),
    );

    expect(screen.getByRole("menuitem", { name: "View details" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Refresh tools" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Disable server|Enable server/ })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Remove server..." })).toBeNull();
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
    // JsonBlock renders the schema as syntax-highlighted tokens, so "title"
    // appears in more than one span (property key + required entry).
    expect(screen.getAllByText(/title/).length).toBeGreaterThan(0);
  });

  it("calls a tool from the detail panel and renders result JSON", async () => {
    const user = userEvent.setup();
    const callTool = vi.fn(async () => ({
      success: true,
      result: { ok: true },
    }));
    renderMcp(makeProps({ callTool }));

    await user.click(await screen.findByText("gobby-tasks"));
    await user.click(await screen.findByText("create_task"));
    await screen.findByText("create_task schema");

    await user.type(screen.getByRole("textbox", { name: /title/ }), "Fix docs");
    await user.click(screen.getByRole("button", { name: "Call tool" }));

    await waitFor(() => {
      expect(callTool).toHaveBeenCalledWith(
        "gobby-tasks",
        "create_task",
        { title: "Fix docs" },
      );
    });
    expect(screen.getByText(/"ok": true/)).toBeInTheDocument();
  });

  it("refreshes MCP tools from the shared header", async () => {
    const user = userEvent.setup();
    const refreshToolCache = vi.fn(async () => true);
    renderMcp(makeProps({ refreshToolCache }));

    await user.click(screen.getByRole("button", { name: "Refresh MCP tools" }));

    expect(refreshToolCache).toHaveBeenCalledOnce();
  });
});
