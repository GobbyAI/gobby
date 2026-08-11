import {
  describe,
  it,
  expect,
  vi,
  beforeAll,
  beforeEach,
  afterEach,
} from "vitest";
import { render as baseRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../ActivityActionsContext";
import { TasksTab } from "../TasksTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import {
  installResizeObserverMock,
  setupDefaultFetchRoutes,
} from "./TasksTab.setup";

// The tab's Filter / Search / New triggers render in the shared panel header
// in the real layout; mount it alongside the tab so those controls are
// reachable in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) => baseRender(ui, { wrapper: HeaderHarness });

vi.mock("../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: () => {},
}));

beforeAll(() => {
  installResizeObserverMock();
});

vi.mock("../../shared/ResizeHandle", () => ({
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
      const title = row.querySelector("[data-task-row-title]");
      const stage = row.querySelector("[data-task-row-stage]");
      const menu = row.querySelector('[aria-label="Task actions"]');

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

    const title = document.querySelector("[data-task-row-title]");
    expect(title).toHaveClass("flex-1", "truncate");
  });
});
