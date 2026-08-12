import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import { IsolationTargetSelector } from "../IsolationTargetSelector";

let fetchMock: MockFetchInstance;

describe("IsolationTargetSelector", () => {
  beforeEach(() => {
    fetchMock = createMockFetch();
  });

  afterEach(() => {
    fetchMock.restore();
    vi.clearAllMocks();
  });

  it("renders a detached worktree label", async () => {
    fetchMock.mockJsonResponse(/\/api\/source-control\/worktrees/, {
      worktrees: [
        {
          id: "worktree-detached",
          branch_name: null,
          worktree_path: "/repo/detached",
          status: "active",
        },
      ],
    });

    render(
      <IsolationTargetSelector
        isolation="worktree"
        worktreeId="worktree-detached"
        cloneId={null}
        onWorktreeIdChange={vi.fn()}
        onCloneIdChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("option", { name: "detached (worktree)" }),
    ).toBeInTheDocument();
  });

  it("renders a detached clone label", async () => {
    fetchMock.mockJsonResponse(/\/api\/source-control\/clones/, {
      clones: [
        {
          id: "clone-detached",
          branch_name: null,
          clone_path: "/repo/clone-detached",
          status: "active",
        },
      ],
    });

    render(
      <IsolationTargetSelector
        isolation="clone"
        worktreeId={null}
        cloneId="clone-detached"
        onWorktreeIdChange={vi.fn()}
        onCloneIdChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("option", { name: "detached (clone-de)" }),
    ).toBeInTheDocument();
  });
});
