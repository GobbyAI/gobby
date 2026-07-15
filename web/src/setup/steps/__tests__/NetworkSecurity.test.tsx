import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../../utils/state.js";
import { NetworkSecurity } from "../NetworkSecurity.js";

const mocks = vi.hoisted(() => ({
  resolveFirewallScriptPath: vi.fn(),
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

vi.mock("ink-spinner", () => ({ default: () => <span /> }));
vi.mock("../../utils/firewall.js", () => ({
  resolveFirewallScriptPath: mocks.resolveFirewallScriptPath,
}));
vi.mock("../../utils/state.js", () => ({ saveState: mocks.saveState }));

describe("NetworkSecurity", () => {
  beforeEach(() => {
    vi.spyOn(process, "platform", "get").mockReturnValue("darwin");
    mocks.resolveFirewallScriptPath.mockReset();
    mocks.saveState.mockReset();
    mocks.spawnSync.mockReset();
    mocks.resolveFirewallScriptPath.mockReturnValue("/opt/gobby/scripts/setup-firewall.sh");
    mocks.spawnSync.mockReturnValue({ status: 0 });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("executes the installed firewall script directly with sudo", () => {
    const state = {
      ports: { http: 60887, ws: 60888, ui: 60889 },
      completed_step_id: null,
    } as unknown as SetupState;
    const setState = vi.fn((updater: (prev: SetupState) => SetupState) => updater(state));
    mocks.spawnSync.mockImplementation(() => {
      expect(screen.getByText(/Configuring firewall rules/i)).toBeTruthy();
      return { status: 0 };
    });

    render(<NetworkSecurity state={state} setState={setState} onNext={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Yes, configure firewall" }));

    expect(mocks.spawnSync).toHaveBeenCalledWith(
      "sudo",
      ["bash", "/opt/gobby/scripts/setup-firewall.sh", "60887", "60888", "60889"],
      { stdio: "inherit", timeout: 60000 },
    );
  });

  it("keeps firewall failures visible without completing the step", () => {
    mocks.spawnSync.mockReturnValue({ status: 1 });
    const state = {
      ports: { http: 60887, ws: 60888, ui: 60889 },
      completed_step_id: null,
    } as unknown as SetupState;
    const setState = vi.fn();
    const onNext = vi.fn();

    render(<NetworkSecurity state={state} setState={setState} onNext={onNext} />);
    fireEvent.click(screen.getByRole("button", { name: "Yes, configure firewall" }));

    expect(screen.getByText(/Firewall setup command failed/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Continue without firewall" })).toBeTruthy();
    expect(setState).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
  });
});
