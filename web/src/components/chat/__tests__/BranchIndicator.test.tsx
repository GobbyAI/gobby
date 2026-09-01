import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NO_CHECKOUT_MESSAGE } from "../../../lib/projectCheckout";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import { BranchIndicator } from "../BranchIndicator";

const currentBranch = {
  name: "current",
  is_current: true,
  is_remote: false,
  worktree_id: null,
};

const localBranch = {
  name: "feature",
  is_current: false,
  is_remote: false,
  worktree_id: null,
};

const remoteBranch = {
  name: "remote-only",
  is_current: false,
  is_remote: true,
  worktree_id: null,
};

let fetchMock: MockFetchInstance;

function mockBranchPickerData(options?: {
  checkoutStatus?: number;
  checkoutBody?: unknown;
  projectCheckout?: {
    machine_id: string;
    root_path: string;
  } | null;
  worktrees?: Array<{
    id: string;
    branch_name: string | null;
    worktree_path: string;
  }>;
}) {
  fetchMock.mockJsonResponse(
    /\/api\/source-control\/status\?project_id=proj-1$/,
    {
      current_branch: "current",
      repo_path: "/status-repo",
    },
  );
  fetchMock.mockJsonResponse(/\/api\/projects\/proj-1\/checkouts$/, {
    checkout:
      options?.projectCheckout === undefined
        ? { machine_id: "machine-1", root_path: "/repo" }
        : options.projectCheckout,
  });
  fetchMock.mockJsonResponse(
    /\/api\/source-control\/worktrees\?project_id=proj-1$/,
    {
      worktrees: options?.worktrees ?? [],
    },
  );
  fetchMock.mockJsonResponse(
    /\/api\/source-control\/branches\?project_id=proj-1$/,
    {
      current_branch: "current",
      branches: [currentBranch, localBranch, remoteBranch],
    },
  );
  fetchMock.mockJsonResponse(
    /\/api\/source-control\/branches\/checkout\?project_id=proj-1$/,
    options?.checkoutBody ?? {
      success: true,
      current_branch: "feature",
      repo_path: "/checkout-response-repo",
    },
    { status: options?.checkoutStatus ?? 200 },
  );
}

async function openBranchPicker(
  onWorktreeChange = vi.fn(),
  worktreePath: string | null = "/repo",
) {
  const user = userEvent.setup();
  render(
    <BranchIndicator
      currentBranch="current"
      worktreePath={worktreePath}
      projectId="proj-1"
      onWorktreeChange={onWorktreeChange}
    />,
  );

  await user.click(screen.getByRole("button", { name: /current/i }));
  await screen.findByRole("option", { name: /feature/i });
  return { user, onWorktreeChange };
}

describe("BranchIndicator", () => {
  beforeEach(() => {
    fetchMock = createMockFetch();
  });

  afterEach(() => {
    fetchMock.restore();
    vi.clearAllMocks();
  });

  it("checks out a local branch before switching chat to the main repo path", async () => {
    mockBranchPickerData();
    const { user, onWorktreeChange } = await openBranchPicker();

    await user.click(screen.getByRole("option", { name: /feature/i }));

    await waitFor(() => {
      expect(onWorktreeChange).toHaveBeenCalledWith("/repo");
    });

    const checkoutCall = fetchMock.fn.mock.calls.find(([input]) =>
      String(input).includes("/api/source-control/branches/checkout"),
    );
    expect(checkoutCall?.[0]).toBe(
      "/api/source-control/branches/checkout?project_id=proj-1",
    );
    expect(checkoutCall?.[1]).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_name: "feature" }),
    });
  });

  it("renders a path-free state when the project has no checkout", async () => {
    mockBranchPickerData({ projectCheckout: null });

    const { user, onWorktreeChange } = await openBranchPicker(vi.fn(), null);

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(NO_CHECKOUT_MESSAGE);
    expect(status).not.toHaveTextContent("/repo");
    expect(screen.queryByRole("alert")).toBeNull();

    // The listbox holds only options and groups; notices sit beside it.
    const listbox = screen.getByRole("listbox");
    expect(within(listbox).queryByRole("status")).toBeNull();
    for (const child of Array.from(listbox.children)) {
      expect(["group", "option"]).toContain(child.getAttribute("role"));
    }

    // A branch switch has nowhere to run, so it is refused with the same copy.
    await user.click(screen.getByRole("option", { name: /feature/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      NO_CHECKOUT_MESSAGE,
    );
    expect(onWorktreeChange).not.toHaveBeenCalled();
    expect(
      fetchMock.fn.mock.calls.some(([input]) =>
        String(input).includes("/api/source-control/branches/checkout"),
      ),
    ).toBe(false);
  });

  it("shows a neutral placeholder while the checkout lookup is pending", async () => {
    mockBranchPickerData();
    const routed = fetchMock.fn.getMockImplementation();
    if (!routed) throw new Error("mock fetch has no implementation");
    fetchMock.fn.mockImplementation((input: RequestInfo | URL) =>
      String(input).includes("/checkouts")
        ? new Promise<Response>(() => {})
        : routed(input),
    );

    await openBranchPicker();

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Looking up checkout...");
    expect(status).not.toHaveTextContent(NO_CHECKOUT_MESSAGE);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      within(screen.getByRole("listbox")).queryByRole("status"),
    ).toBeNull();
  });

  it("reports a failed checkout lookup instead of claiming there is none", async () => {
    // Registered first so it wins over the default 200 route.
    fetchMock.mockJsonResponse(
      /\/api\/projects\/proj-1\/checkouts$/,
      {
        detail: {
          error: "MissingMachineContextError",
          message: "machine context missing",
        },
      },
      { status: 409 },
    );
    mockBranchPickerData();

    await openBranchPicker();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("machine context missing");
    expect(alert).toHaveClass("text-destructive-foreground");
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(NO_CHECKOUT_MESSAGE)).toBeNull();
    expect(within(screen.getByRole("listbox")).queryByRole("alert")).toBeNull();
  });

  it("does not render remote-only branches as switch targets", async () => {
    mockBranchPickerData();

    await openBranchPicker();

    expect(
      screen.getByRole("option", { name: /feature/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("option", { name: /remote-only/i }),
    ).not.toBeInTheDocument();
  });

  it("renders detached worktrees without hiding same-named branches", async () => {
    mockBranchPickerData({
      worktrees: [
        {
          id: "worktree-detached",
          branch_name: null,
          worktree_path: "/repo/detached",
        },
      ],
    });
    const { user, onWorktreeChange } = await openBranchPicker();

    expect(
      screen.getByRole("option", { name: /detached/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: /feature/i }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("option", { name: /detached/i }));
    expect(onWorktreeChange).toHaveBeenCalledWith(
      "/repo/detached",
      "worktree-detached",
    );
  });

  it("shows checkout errors without switching worktrees", async () => {
    mockBranchPickerData({
      checkoutStatus: 409,
      checkoutBody: { detail: "dirty worktree" },
    });
    const { user, onWorktreeChange } = await openBranchPicker();

    await user.click(screen.getByRole("option", { name: /feature/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("dirty worktree");
    expect(alert).toHaveClass("text-destructive-foreground");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(onWorktreeChange).not.toHaveBeenCalled();
  });
});
