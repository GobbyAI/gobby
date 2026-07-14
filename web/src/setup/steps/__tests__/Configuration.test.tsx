import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../../utils/state.js";
import { Configuration } from "../Configuration.js";

const mocks = vi.hoisted(() => ({
  runGobby: vi.fn(),
  saveState: vi.fn(),
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

vi.mock("ink-text-input", () => ({ default: () => <input /> }));
vi.mock("../../utils/config.js", () => ({ patchPorts: vi.fn() }));
vi.mock("../../utils/gobby.js", () => ({ runGobby: mocks.runGobby }));
vi.mock("../../utils/state.js", () => ({ saveState: mocks.saveState }));

function createState(): SetupState {
  return {
    ports: { http: 60887, ws: 60888, ui: 60889 },
    completed_step_id: null,
  } as unknown as SetupState;
}

function renderConfiguration() {
  let state = createState();
  const onNext = vi.fn();
  const setState = vi.fn((updater: (prev: SetupState) => SetupState) => {
    state = updater(state);
  });

  render(<Configuration state={state} setState={setState} onNext={onNext} />);
  return { onNext, setState };
}

describe("Configuration", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.runGobby.mockReset();
    mocks.saveState.mockReset();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("runs config-only initialization and completes after success", () => {
    mocks.runGobby.mockReturnValue({ success: true, output: "" });
    const view = renderConfiguration();

    fireEvent.click(screen.getByRole("button", { name: "No, use defaults" }));

    expect(mocks.runGobby).toHaveBeenCalledWith(["install", "--config-only"], {
      timeout: 30000,
    });
    expect(view.setState).toHaveBeenCalledOnce();
    expect(mocks.saveState).toHaveBeenCalledWith(
      expect.objectContaining({ completed_step_id: "config" }),
    );
    expect(screen.getByText(/Configuration saved/i)).toBeTruthy();
  });

  it("keeps the step incomplete on failure and retries", () => {
    mocks.runGobby
      .mockReturnValueOnce({ success: false, output: "database unavailable" })
      .mockReturnValueOnce({ success: true, output: "" });
    const view = renderConfiguration();

    fireEvent.click(screen.getByRole("button", { name: "No, use defaults" }));

    expect(screen.getByText(/Configuration failed: database unavailable/i)).toBeTruthy();
    expect(view.setState).not.toHaveBeenCalled();
    expect(view.onNext).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(mocks.runGobby).toHaveBeenCalledTimes(2);
    expect(view.setState).toHaveBeenCalledOnce();
    expect(screen.getByText(/Configuration saved/i)).toBeTruthy();
  });

  it("surfaces a timeout when the command returns no output", () => {
    mocks.runGobby.mockReturnValue({ success: false, output: "" });
    const view = renderConfiguration();

    fireEvent.click(screen.getByRole("button", { name: "No, use defaults" }));

    expect(screen.getByText(/failed or timed out/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Exit setup" })).toBeTruthy();
    expect(view.setState).not.toHaveBeenCalled();
  });
});
