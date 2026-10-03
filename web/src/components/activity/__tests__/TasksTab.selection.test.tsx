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
  taskList,
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

it("selects the task a focus ref resolves to, then reports it handled", async () => {
  // Routes match first-wins, so the ref route must precede the default detail route.
  mockFetch.resetRoutes();
  mockFetch.mockJsonResponse("/api/tasks/%23412", { task: taskList[3] });
  setupDefaultFetchRoutes(mockFetch);
  const onFocusHandled = vi.fn();

  render(
    <TasksTab
      projectId="proj-1"
      focusTaskRef="#412"
      onFocusHandled={onFocusHandled}
    />,
  );

  const row = (title: string) =>
    screen.getByText(title).closest('[role="treeitem"]');
  await vi.waitFor(() => {
    expect(row("Open task 3")).toHaveAttribute("aria-selected", "true");
  });
  expect(row("Review approved task")).toHaveAttribute("aria-selected", "false");
  expect(onFocusHandled).toHaveBeenCalledOnce();
});

it("keeps a focused task the default filters hide, showing its detail", async () => {
  // Feedback runs are history: their filed tasks are routinely closed, and the
  // default filters exclude closed rows.
  const closedTask = taskList.find((task) => task.id === "task-closed");
  mockFetch.resetRoutes();
  mockFetch.mockJsonResponse("/api/tasks/%23499", { task: closedTask });
  mockFetch.mockJsonResponse("/api/tasks/task-closed", { task: closedTask });
  setupDefaultFetchRoutes(mockFetch);

  render(<TasksTab projectId="proj-1" focusTaskRef="#499" />);

  expect(await screen.findByDisplayValue("Closed task")).toBeInTheDocument();
  // Let the list load and the reconcile effect run against the hidden row.
  await act(async () => {});
  expect(screen.getByDisplayValue("Closed task")).toBeInTheDocument();
  expect(
    screen.getByText("Review approved task").closest('[role="treeitem"]'),
  ).toHaveAttribute("aria-selected", "false");
});

it("asks for a retry when a focus ref fails to load", async () => {
  mockFetch.resetRoutes();
  mockFetch.mockErrorResponse("/api/tasks/%23412", 500);
  setupDefaultFetchRoutes(mockFetch);

  render(<TasksTab projectId="proj-1" focusTaskRef="#412" />);

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn't load task #412. Try again.",
  );
});

it("says so when a focus ref does not resolve", async () => {
  mockFetch.resetRoutes();
  mockFetch.mockErrorResponse("/api/tasks/%23999", 404);
  setupDefaultFetchRoutes(mockFetch);
  const onFocusHandled = vi.fn();

  render(
    <TasksTab
      projectId="proj-1"
      focusTaskRef="#999"
      onFocusHandled={onFocusHandled}
    />,
  );

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Task #999 was not found.",
  );
  expect(onFocusHandled).toHaveBeenCalledOnce();
});
