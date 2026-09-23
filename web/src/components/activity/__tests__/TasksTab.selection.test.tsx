import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { TasksTab } from "../TasksTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import {
  installResizeObserverMock,
  setupDefaultFetchRoutes,
} from "./TasksTab.setup";

vi.mock("../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: () => {},
}));

vi.mock("../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

// Runs after each list commit's DOM mutations and before that commit's
// passive effects flush, which is where a real click can land when React
// yields between the two.
const onListCommit = vi.hoisted(() => ({
  current: null as (() => void) | null,
}));

vi.mock("../TasksTabList", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../TasksTabList")>();
  const { useLayoutEffect } = await import("react");
  return {
    ...actual,
    TasksTabList: (props: Parameters<typeof actual.TasksTabList>[0]) => {
      useLayoutEffect(() => onListCommit.current?.());
      return <actual.TasksTabList {...props} />;
    },
  };
});

let mockFetch: MockFetchInstance;

beforeAll(() => {
  installResizeObserverMock();
});

beforeEach(() => {
  mockFetch = createMockFetch();
  setupDefaultFetchRoutes(mockFetch);
});

afterEach(() => {
  onListCommit.current = null;
  mockFetch.restore();
});

it("keeps a row clicked before the first-row default selection is applied", async () => {
  onListCommit.current = () => {
    const row = screen.queryByText("Open task 2");
    if (!row) return;
    onListCommit.current = null;
    row.click();
  };

  render(<TasksTab projectId="proj-1" />);
  // Let the task list fetch resolve inside act so every render and effect it
  // triggers has flushed before the assertions read the settled selection.
  await act(async () => {});

  const row = (title: string) =>
    screen.getByText(title).closest('[role="treeitem"]');
  expect(row("Open task 2")).toHaveAttribute("aria-selected", "true");
  expect(row("Review approved task")).toHaveAttribute("aria-selected", "false");
});
