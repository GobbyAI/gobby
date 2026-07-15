import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../../utils/state.js";
import { Launch } from "../Launch.js";

const mocks = vi.hoisted(() => ({
  checkHealth: vi.fn(),
  execSync: vi.fn(),
  runGobby: vi.fn(),
  saveState: vi.fn(),
  writeFileSync: vi.fn(),
}));

vi.mock("child_process", () => ({
  default: { execSync: mocks.execSync },
  execSync: mocks.execSync,
}));

vi.mock("fs", () => ({
  default: { writeFileSync: mocks.writeFileSync },
  writeFileSync: mocks.writeFileSync,
}));

vi.mock("ink", () => ({
  Box: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children?: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("ink-spinner", () => ({
  default: () => <span data-testid="spinner" />,
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

vi.mock("../../utils/gobby.js", () => ({
  checkHealth: mocks.checkHealth,
  runGobby: mocks.runGobby,
}));

vi.mock("../../utils/state.js", () => ({
  getGobbyHome: () => "/tmp/gobby-home",
  saveState: mocks.saveState,
}));

function createState(): SetupState {
  return {
    version: 3,
    started_at: "2026-05-22T00:00:00.000Z",
    completed_at: null,
    completed_step_id: "services",
    user_name: "Ada",
    ports: { http: 60887, ws: 60888, ui: 60889 },
    detected_tools: {},
    tool_versions: {},
    installed_clis: [],
    projects: [],
    firewall_configured: false,
    tailscale_configured: false,
    secrets_configured: [],
    falkordb_installed: true,
    falkordb_password_set: true,
    personal_dir_created: false,
    desktop_shortcut_created: false,
  } as unknown as SetupState;
}

describe("Launch summary", () => {
  beforeEach(() => {
    mocks.checkHealth.mockReset();
    mocks.execSync.mockReset();
    mocks.runGobby.mockReset();
    mocks.saveState.mockReset();
    mocks.writeFileSync.mockReset();
    mocks.checkHealth.mockResolvedValue(true);
    mocks.runGobby.mockReturnValue({ success: true, output: "" });
  });

  it("writes the setup summary from falkordb state fields", async () => {
    const setState = vi.fn((updater: (prev: SetupState) => SetupState) => {
      updater(createState());
    });

    render(<Launch state={createState()} setState={setState} onNext={vi.fn()} />);

    await waitFor(() => expect(mocks.writeFileSync).toHaveBeenCalled());
    const summary = mocks.writeFileSync.mock.calls[0][1] as string;

    expect(summary).toContain("## Services");
    expect(summary).toContain("- FalkorDB: installed (Docker)");
    expect(summary).toContain("- FalkorDB password: custom");
    expect(summary).not.toContain("Neo4j");
  });

  it("keeps launch incomplete when gobby start fails and supports retry", async () => {
    mocks.runGobby
      .mockReturnValueOnce({ success: false, output: "daemon refused to start" })
      .mockReturnValueOnce({ success: true, output: "" });
    const setState = vi.fn();

    render(<Launch state={createState()} setState={setState} onNext={vi.fn()} />);

    expect(await screen.findByText(/Launch failed: daemon refused to start/i)).toBeTruthy();
    expect(mocks.checkHealth).not.toHaveBeenCalled();
    expect(mocks.writeFileSync).not.toHaveBeenCalled();
    expect(mocks.execSync).not.toHaveBeenCalled();
    expect(setState).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText(/Setup complete!/i)).toBeTruthy();
    expect(mocks.runGobby).toHaveBeenCalledTimes(2);
    expect(mocks.checkHealth).toHaveBeenCalledOnce();
    expect(setState).toHaveBeenCalledOnce();
  });

  it("does not complete or open the browser when health never passes", async () => {
    mocks.checkHealth.mockResolvedValue(false);
    const setState = vi.fn();

    render(<Launch state={createState()} setState={setState} onNext={vi.fn()} />);

    expect(await screen.findByText(/Launch failed: Daemon health check did not pass/i)).toBeTruthy();
    expect(mocks.writeFileSync).not.toHaveBeenCalled();
    expect(mocks.execSync).not.toHaveBeenCalled();
    expect(setState).not.toHaveBeenCalled();
    expect(screen.queryByText(/Setup complete!/i)).toBeNull();
  });
});
