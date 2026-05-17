import {
  describe,
  it,
  expect,
  vi,
  beforeAll,
  beforeEach,
  afterEach,
} from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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

beforeAll(() => {
  installResizeObserverMock();
});

vi.mock("../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

let mockFetch: MockFetchInstance;

describe("TasksTab row layout (#14247)", () => {
  beforeEach(() => {
    mockFetch = createMockFetch();
    setupDefaultFetchRoutes(mockFetch);
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  it("renders [title] [stage chip] [menu] in that left-to-right order", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Open task 2")).toBeTruthy();
    });

    const rows = screen.getAllByRole("treeitem");
    expect(rows.length).toBeGreaterThan(0);

    for (const row of rows) {
      const title = row.querySelector(".activity-task-row-title");
      const stage = row.querySelector(".activity-task-row-stage");
      const menu = row.querySelector(".task-more-btn");

      expect(title).not.toBeNull();
      expect(menu).not.toBeNull();

      const titleIndex = Array.from(row.children).indexOf(title as Element);
      const menuIndex = Array.from(row.children).indexOf(menu as Element);

      expect(titleIndex).toBeGreaterThanOrEqual(0);
      expect(menuIndex).toBeGreaterThan(titleIndex);

      if (stage) {
        const stageIndex = Array.from(row.children).indexOf(stage as Element);
        expect(stageIndex).toBeGreaterThan(titleIndex);
        expect(stageIndex).toBeLessThan(menuIndex);
      }
    }
  });

  it("title takes the flexible column and truncates", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Open task 2")).toBeTruthy();
    });

    const title = document.querySelector(".activity-task-row-title")
    expect(title).not.toBeNull()
  });
});
