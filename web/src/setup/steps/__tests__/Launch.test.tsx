import React from "react";
import { render, waitFor } from "@testing-library/react";
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
});
