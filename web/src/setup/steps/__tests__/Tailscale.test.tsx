import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../../utils/state.js";
import { Tailscale } from "../Tailscale.js";

const mocks = vi.hoisted(() => ({
  saveState: vi.fn(),
  setBindHost: vi.fn(),
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

vi.mock("ink-spinner", () => ({ default: () => <span /> }));
vi.mock("../../utils/config.js", () => ({ setBindHost: mocks.setBindHost }));
vi.mock("../../utils/state.js", () => ({ saveState: mocks.saveState }));

describe("Tailscale", () => {
  beforeEach(() => {
    mocks.saveState.mockReset();
    mocks.setBindHost.mockReset();
    mocks.spawnSync.mockReset();
    mocks.spawnSync.mockReturnValue({ status: 0 });
  });

  it("configures tailscale serve without changing the daemon bind host", () => {
    const state = {
      ports: { http: 60887, ws: 60888, ui: 60889 },
      completed_step_id: null,
    } as unknown as SetupState;
    const setState = vi.fn((updater: (prev: SetupState) => SetupState) => updater(state));
    mocks.spawnSync.mockImplementation(() => {
      expect(screen.getByText(/Configuring Tailscale serve/i)).toBeTruthy();
      return { status: 0 };
    });

    render(<Tailscale state={state} setState={setState} onNext={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Yes, configure tailscale serve" }));

    expect(mocks.spawnSync).toHaveBeenCalledWith(
      "tailscale",
      ["serve", "--bg", "60889"],
      { encoding: "utf-8", timeout: 30000 },
    );
    expect(mocks.setBindHost).not.toHaveBeenCalled();
    expect(mocks.saveState).toHaveBeenCalledWith(
      expect.objectContaining({
        tailscale_configured: true,
        completed_step_id: "tailscale",
      }),
    );
  });

  it("keeps tailscale failures visible without completing the step", () => {
    mocks.spawnSync.mockReturnValue({ status: 1, stderr: "permission denied" });
    const state = {
      ports: { http: 60887, ws: 60888, ui: 60889 },
      completed_step_id: null,
    } as unknown as SetupState;
    const setState = vi.fn();
    const onNext = vi.fn();

    render(<Tailscale state={state} setState={setState} onNext={onNext} />);
    fireEvent.click(screen.getByRole("button", { name: "Yes, configure tailscale serve" }));

    expect(screen.getByText(/Tailscale setup failed: permission denied/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Continue without Tailscale" })).toBeTruthy();
    expect(setState).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
  });
});
