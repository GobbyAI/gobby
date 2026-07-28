import { useState, type ReactNode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  return render(<Harness props={props}><ActivityActionButtons /></Harness>);
}

function treeItemFor(text: string): HTMLElement {
  const tree = screen.getByRole("tree", { name: "MCP servers and tools" });
  return within(tree)
    .getAllByText(text)[0]
    .closest('[role="treeitem"]') as HTMLElement;
}

describe("ActivityMcpTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
        if (String(input) === "/api/projects") {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        throw new Error(`Unhandled fetch: ${String(input)}`);
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a two-level ARIA tree and expands via the chevron", async () => {
    const user = userEvent.setup();
    renderMcp();

    await screen.findByText("gobby-tasks");
    const serverRow = treeItemFor("gobby-tasks");
    expect(serverRow).toHaveAttribute("role", "treeitem");
    expect(serverRow).toHaveAttribute("aria-level", "1");
    expect(serverRow).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("list_tasks")).toBeNull();

    await user.click(
      within(serverRow).getByRole("button", {
        name: "Expand gobby-tasks tools",
      }),
    );

    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-expanded", "true");
    const toolRow = treeItemFor("list_tasks");
    expect(toolRow).toHaveAttribute("role", "treeitem");
    expect(toolRow).toHaveAttribute("aria-level", "2");
  });

  it("selects a server row on click without toggling expansion", async () => {
    const user = userEvent.setup();
    renderMcp();

    const serverRow = await screen.findByText("gobby-tasks").then(() =>
      treeItemFor("gobby-tasks"),
    );
    await user.click(serverRow);

    // Clicking the row selects it (canonical tree semantics) but does not expand.
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-selected", "true");
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("list_tasks")).toBeNull();
  });

  it("navigates the tree with arrow keys and activates with Enter", async () => {
    const fetchToolSchema = vi.fn(makeProps().fetchToolSchema);
    renderMcp(makeProps({ fetchToolSchema }));

    await screen.findByText("gobby-tasks");
    const serverRow = treeItemFor("gobby-tasks");
    // The default-selected first row (github, alphabetical) is the tree's tab
    // entry point (#19152).
    expect(treeItemFor("github")).toHaveAttribute("tabindex", "0");
    serverRow.focus();
    expect(document.activeElement).toBe(serverRow);

    // ArrowRight expands a collapsed server.
    fireEvent.keyDown(serverRow, { key: "ArrowRight" });
    await waitFor(() => expect(document.activeElement).toBe(serverRow));
    expect(serverRow).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("list_tasks")).toBeInTheDocument();

    // ArrowRight again moves the roving anchor into the first tool.
    fireEvent.keyDown(serverRow, { key: "ArrowRight" });
    const toolRow = treeItemFor("list_tasks");
    await waitFor(() => expect(document.activeElement).toBe(toolRow));
    expect(toolRow).toHaveAttribute("tabindex", "0");
    expect(serverRow).toHaveAttribute("tabindex", "-1");

    // Enter activates the focused tool (loads its schema).
    fireEvent.keyDown(toolRow, { key: "Enter" });
    await waitFor(() => {
      expect(fetchToolSchema).toHaveBeenCalledWith("gobby-tasks", "list_tasks");
    });

    // ArrowLeft from a leaf returns the anchor to the parent server.
    fireEvent.keyDown(toolRow, { key: "ArrowLeft" });
    await waitFor(() => expect(document.activeElement).toBe(serverRow));
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("tabindex", "0");
  });

  it("activates nested controls via keyboard without the row swallowing keys", async () => {
    const user = userEvent.setup();
    renderMcp();

    await screen.findByText("gobby-tasks");
    const serverRow = treeItemFor("gobby-tasks");
    const chevron = within(serverRow).getByRole("button", {
      name: "Expand gobby-tasks tools",
    });

    chevron.focus();
    await user.keyboard("{Enter}");

    // The chevron toggled expansion, and the row was NOT also selected — i.e.
    // the treeitem keydown handler did not swallow the Enter meant for the button.
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-expanded", "true");
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("list_tasks")).toBeInTheDocument();
  });

  it("opens the kebab menu via keyboard without selecting the row", async () => {
    const user = userEvent.setup();
    renderMcp();

    await screen.findByText("gobby-tasks");
    const serverRow = treeItemFor("gobby-tasks");
    const kebab = within(serverRow).getByRole("button", {
      name: "Open actions for gobby-tasks server",
    });

    kebab.focus();
    await user.keyboard("{Enter}");

    expect(
      screen.getByRole("menuitem", { name: "View details" }),
    ).toBeInTheDocument();
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-selected", "false");
  });

  it("auto-expands matching servers on search but still allows collapse", async () => {
    const user = userEvent.setup();
    renderMcp();

    // The search bar is hidden until the header Search toggle opens it.
    await user.click(screen.getByRole("button", { name: "Search MCP" }));
    await user.type(screen.getByPlaceholderText("Search MCP"), "list");

    // "list" matches gobby-tasks via its list_tasks tool, which auto-expands.
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("list_tasks")).toBeInTheDocument();

    // Collapsing mid-search sticks (does not immediately re-expand).
    await user.click(
      within(treeItemFor("gobby-tasks")).getByRole("button", {
        name: "Collapse gobby-tasks tools",
      }),
    );
    expect(treeItemFor("gobby-tasks")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("list_tasks")).toBeNull();
  });

  it("searches server names, tool names, and tool briefs", async () => {
    const user = userEvent.setup();
    renderMcp();

    await user.click(screen.getByRole("button", { name: "Search MCP" }));
    await user.type(screen.getByPlaceholderText("Search MCP"), "issue");

    const tree = screen.getByRole("tree", { name: "MCP servers and tools" });
    expect(within(tree).getByText("github")).toBeInTheDocument();
    expect(within(tree).getByText("create_issue")).toBeInTheDocument();
    expect(within(tree).queryByText("gobby-tasks")).toBeNull();
  });

  it("filters by server type with the All | Internal | External selector", async () => {
    const user = userEvent.setup();
    renderMcp();

    const group = screen.getByRole("radiogroup", { name: "MCP server type" });
    expect(within(group).getByRole("radio", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    const tree = screen.getByRole("tree", { name: "MCP servers and tools" });
    await user.click(within(group).getByRole("radio", { name: "Internal" }));
    expect(within(tree).getByText("gobby-tasks")).toBeInTheDocument();
    expect(within(tree).queryByText("github")).toBeNull();

    await user.click(within(group).getByRole("radio", { name: "External" }));
    expect(within(tree).getByText("github")).toBeInTheDocument();
    expect(within(tree).queryByText("gobby-tasks")).toBeNull();
  });

  it("sorts servers alphabetically and default-selects the first server (#19152)", async () => {
    renderMcp();

    const tree = await screen.findByRole("tree", { name: "MCP servers and tools" });
    const rows = within(tree).getAllByRole("treeitem");
    expect(rows[0]).toHaveAccessibleName("github server, External");
    await waitFor(() => expect(rows[0]).toHaveAttribute("aria-selected", "true"));
  });

  it("opens a new-server detail draft from the shared header", async () => {
    const user = userEvent.setup();
    renderMcp();

    await user.click(screen.getByRole("button", { name: "New MCP server" }));

    expect(screen.queryByRole("heading", { name: "Add MCP Server" })).toBeNull();
    expect(screen.getByText("New MCP server")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Server name" })).toBeInTheDocument();
    expect(screen.getByLabelText("Project")).toBeInTheDocument();
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

    await screen.findByText("gobby-tasks");
    await user.click(
      within(treeItemFor("gobby-tasks")).getByRole("button", {
        name: "Expand gobby-tasks tools",
      }),
    );
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

    await screen.findByText("gobby-tasks");
    await user.click(
      within(treeItemFor("gobby-tasks")).getByRole("button", {
        name: "Expand gobby-tasks tools",
      }),
    );
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

  it("discards a tool result after selecting another tool", async () => {
    const user = userEvent.setup();
    let resolveCall: ((value: { success: boolean; result: unknown }) => void) | null =
      null;
    const callTool = vi.fn(
      () =>
        new Promise<{ success: boolean; result: unknown }>((resolve) => {
          resolveCall = resolve;
        }),
    );
    renderMcp(makeProps({ callTool }));

    await screen.findByText("gobby-tasks");
    await user.click(
      within(treeItemFor("gobby-tasks")).getByRole("button", {
        name: "Expand gobby-tasks tools",
      }),
    );
    await user.click(await screen.findByText("create_task"));
    await screen.findByText("create_task schema");
    await user.type(screen.getByRole("textbox", { name: /title/ }), "Fix docs");
    await user.click(screen.getByRole("button", { name: "Call tool" }));
    await waitFor(() => expect(callTool).toHaveBeenCalledOnce());

    await user.click(screen.getByText("list_tasks"));
    await screen.findByText("list_tasks schema");
    await act(async () => {
      resolveCall?.({ success: true, result: { stale: true } });
    });

    expect(screen.queryByText(/"stale": true/)).toBeNull();
  });

  it("refreshes MCP tools from the server menu, with no header refresh button (#19152)", async () => {
    const user = userEvent.setup();
    const refreshToolCache = vi.fn(async () => true);
    renderMcp(makeProps({ refreshToolCache }));

    expect(screen.queryByRole("button", { name: "Refresh MCP tools" })).toBeNull();

    await user.click(
      screen.getByRole("button", { name: "Open actions for gobby-tasks server" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Refresh tools" }));

    await waitFor(() => {
      expect(refreshToolCache).toHaveBeenCalledOnce();
    });
  });
});
