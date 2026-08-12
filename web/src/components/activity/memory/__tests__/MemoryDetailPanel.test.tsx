import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { GobbyMemory } from "../../../../hooks/useMemory";
import { MemoryDetailPanel } from "../MemoryDetailPanel";

function makeMemory(overrides: Partial<GobbyMemory> = {}): GobbyMemory {
  return {
    id: "mem-hidden",
    memory_type: "fact",
    content: "Hidden memory",
    created_at: "2026-06-14T00:00:00Z",
    updated_at: "2026-06-14T00:00:00Z",
    project_id: "proj-1",
    is_global: false,
    source_type: "agent",
    source_session_id: null,
    importance: 0.5,
    access_count: 0,
    last_accessed_at: null,
    tags: [],
    deleted_at: "2026-06-14T00:00:00Z",
    dream_action: "review",
    last_dreamed_at: "2026-06-14T00:00:00Z",
    ...overrides,
  };
}

describe("MemoryDetailPanel", () => {
  it("sets restoring state before invoking restore callback", async () => {
    const labelsSeenByRestore: string[] = [];
    const onRestore = vi.fn(() => {
      labelsSeenByRestore.push(
        screen.getByRole("button", { name: "Restore memory" }).textContent ??
          "",
      );
    });

    render(
      <MemoryDetailPanel
        memory={makeMemory()}
        onSave={vi.fn(async () => true)}
        onConfirmLeaveChange={vi.fn()}
        onRestore={onRestore}
        purgeGraceDays={{ review: 90, delete: 30 }}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Restore memory" }),
    );

    await waitFor(() => expect(onRestore).toHaveBeenCalledTimes(1));
    expect(labelsSeenByRestore[0]).toContain("Restoring");
  });
});
