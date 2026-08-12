import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ToolBrowserModal } from "../ToolBrowserModal";

const mcpMock = vi.hoisted(() => ({
  fetchToolSchema: vi.fn(),
  fetchTools: vi.fn(),
  fetchServers: vi.fn(),
  callTool: vi.fn(),
}));

vi.mock("../../../hooks/useMcp", () => ({
  useMcp: () => ({
    servers: [{ name: "gobby-tasks", transport: "internal" }],
    toolsByServer: {
      "gobby-tasks": [
        { name: "tool-a", brief: "Tool A" },
        { name: "tool-b", brief: "Tool B" },
      ],
    },
    isLoading: false,
    ...mcpMock,
  }),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("ToolBrowserModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps the selected tool schema when an older request resolves last", async () => {
    const toolA = deferred<{
      description: string;
      inputSchema: { type: string };
    }>();
    const toolB = deferred<{
      description: string;
      inputSchema: { type: string };
    }>();
    mcpMock.fetchToolSchema.mockImplementation(
      (_serverName: string, toolName: string) =>
        toolName === "tool-a" ? toolA.promise : toolB.promise,
    );
    const user = userEvent.setup();

    render(
      <ToolBrowserModal
        filter="internal"
        onClose={vi.fn()}
        onSendMessage={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /tool-a/i }));
    await user.click(screen.getByRole("button", { name: /tool-b/i }));

    await act(async () => {
      toolB.resolve({
        description: "Schema for B",
        inputSchema: { type: "object" },
      });
    });
    expect(screen.getByText("Schema for B")).toBeInTheDocument();

    await act(async () => {
      toolA.resolve({
        description: "Schema for A",
        inputSchema: { type: "object" },
      });
    });
    expect(screen.getByText("Schema for B")).toBeInTheDocument();
    expect(screen.queryByText("Schema for A")).not.toBeInTheDocument();
  });
});
