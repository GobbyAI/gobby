import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GobbyMemory } from "../../../../hooks/useMemory";
import { MemoryTabList } from "../MemoryTabList";

function makeMemory(overrides: Partial<GobbyMemory> = {}): GobbyMemory {
  return {
    id: "mem-1",
    memory_type: "fact",
    content: "Memory content",
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
    deleted_at: null,
    dream_action: null,
    last_dreamed_at: null,
    ...overrides,
  };
}

describe("MemoryTabList", () => {
  it("renders preview content in row titles", () => {
    const fullContent = "a".repeat(141);
    const preview = `${"a".repeat(140)}...`;

    render(
      <MemoryTabList
        memories={[makeMemory({ content: fullContent })]}
        selectedId={null}
        busyId={null}
        onSelect={vi.fn()}
        onCopy={vi.fn()}
        onDelete={vi.fn()}
        onRestore={vi.fn()}
      />,
    );

    const row = screen.getByRole("listitem");
    expect(
      within(row).getByText(preview, { selector: ".activity-row-title" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(fullContent)).not.toBeInTheDocument();
  });
});
