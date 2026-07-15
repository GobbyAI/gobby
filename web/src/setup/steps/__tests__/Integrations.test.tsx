import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../../utils/state.js";
import { Integrations } from "../Integrations.js";

const mocks = vi.hoisted(() => ({
  saveState: vi.fn(),
  spawnSync: vi.fn(),
}));

vi.mock("child_process", () => ({
  default: { spawnSync: mocks.spawnSync },
  spawnSync: mocks.spawnSync,
}));

vi.mock("ink", () => ({
  Box: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children?: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("ink-select-input", () => ({
  default: ({
    items,
    onSelect,
  }: {
    items: Array<{ label: string; value: string }>;
    onSelect: (item: { label: string; value: string }) => void;
  }) => (
    <div>
      {items.map((item) => (
        <button key={item.value} onClick={() => onSelect(item)}>
          {item.label}
        </button>
      ))}
    </div>
  ),
}));

vi.mock("ink-text-input", () => ({
  default: ({ onSubmit }: { onSubmit: (value: string) => void }) => (
    <button onClick={() => onSubmit("test-secret")}>Submit secret</button>
  ),
}));

vi.mock("../../utils/state.js", () => ({ saveState: mocks.saveState }));

function createState(): SetupState {
  return {
    completed_step_id: null,
    secrets_configured: [],
  } as unknown as SetupState;
}

describe("Integrations", () => {
  beforeEach(() => {
    mocks.saveState.mockReset();
    mocks.spawnSync.mockReset();
  });

  it("renders a secret-store failure in the active input phase", () => {
    mocks.spawnSync.mockImplementation(() => {
      expect(screen.getByText(/Saving GitHub key/i)).toBeTruthy();
      return { status: 1 };
    });
    const setState = vi.fn();
    const onNext = vi.fn();

    render(<Integrations state={createState()} setState={setState} onNext={onNext} />);
    fireEvent.click(screen.getByRole("button", { name: /Yes, enter GitHub API key/i }));
    fireEvent.click(screen.getByRole("button", { name: "Submit secret" }));

    expect(screen.getByText(/Failed to store GitHub secret/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Submit secret" })).toBeTruthy();
    expect(setState).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
  });
});
