import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../utils/state.js";
import { App } from "../App.js";

const mocks = vi.hoisted(() => ({
  checkHealth: vi.fn(),
  detectAllTools: vi.fn(),
  exec: vi.fn(),
  execSync: vi.fn(),
  existsSync: vi.fn(),
  findRepos: vi.fn(),
  isGobbyInstalled: vi.fn(),
  lstatSync: vi.fn(),
  loadState: vi.fn(),
  mkdirSync: vi.fn(),
  patchPorts: vi.fn(),
  runGobby: vi.fn(),
  saveState: vi.fn(),
  spawnSync: vi.fn(),
  symlinkSync: vi.fn(),
  unlinkSync: vi.fn(),
  writeFileSync: vi.fn(),
}));

vi.mock("child_process", () => ({
  default: {
    exec: mocks.exec,
    execSync: mocks.execSync,
    spawnSync: mocks.spawnSync,
  },
  exec: mocks.exec,
  execSync: mocks.execSync,
  spawnSync: mocks.spawnSync,
}));

vi.mock("fs", () => ({
  default: {
    existsSync: mocks.existsSync,
    lstatSync: mocks.lstatSync,
    mkdirSync: mocks.mkdirSync,
    symlinkSync: mocks.symlinkSync,
    unlinkSync: mocks.unlinkSync,
    writeFileSync: mocks.writeFileSync,
  },
  existsSync: mocks.existsSync,
  lstatSync: mocks.lstatSync,
  mkdirSync: mocks.mkdirSync,
  symlinkSync: mocks.symlinkSync,
  unlinkSync: mocks.unlinkSync,
  writeFileSync: mocks.writeFileSync,
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
        <button key={item.value} type="button" onClick={() => onSelect(item)}>
          {item.label}
        </button>
      ))}
    </div>
  ),
}));

vi.mock("ink-spinner", () => ({
  default: () => <span data-testid="spinner" />,
}));

vi.mock("ink-text-input", () => ({
  default: ({
    value,
    onChange,
    onSubmit,
  }: {
    value: string;
    onChange: (value: string) => void;
    onSubmit: (value: string) => void;
  }) => (
    <input
      aria-label="wizard input"
      defaultValue={value}
      onChange={(event) => {
        onChange(event.currentTarget.value);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          onSubmit(event.currentTarget.value);
        }
      }}
    />
  ),
}));

vi.mock("../utils/config.js", () => ({
  patchPorts: mocks.patchPorts,
}));

vi.mock("../utils/detect.js", () => ({
  OPTIONAL_TOOLS: ["claude", "gemini", "qwen", "codex", "droid", "tailscale"],
  REQUIRED_TOOLS: ["python", "node", "uv", "tmux", "git", "docker"],
  detectAllTools: mocks.detectAllTools,
  detectTool: vi.fn(() => "present"),
}));

vi.mock("../utils/gobby.js", () => ({
  checkHealth: mocks.checkHealth,
  isGobbyInstalled: mocks.isGobbyInstalled,
  runGobby: mocks.runGobby,
}));

vi.mock("../utils/repos.js", () => ({
  displayPath: (path: string) => path,
  findRepos: mocks.findRepos,
}));

vi.mock("../utils/state.js", () => ({
  getGobbyHome: () => "/tmp/gobby-home",
  loadState: mocks.loadState,
  saveState: mocks.saveState,
}));

function createState(): SetupState {
  return {
    version: 3,
    started_at: "2026-05-22T00:00:00.000Z",
    completed_at: null,
    completed_step_id: null,
    user_name: null,
    ports: { http: 60887, ws: 60888, ui: 60889 },
    detected_tools: {},
    tool_versions: {},
    installed_clis: [],
    projects: [],
    firewall_configured: false,
    tailscale_configured: false,
    secrets_configured: [],
    falkordb_installed: false,
    falkordb_password_set: false,
    personal_dir_created: false,
    desktop_shortcut_created: false,
  };
}

async function click(label: RegExp): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: label }));
  await flush();
}

async function submit(value: string): Promise<void> {
  const input = screen.getByLabelText("wizard input");
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter" });
  await flush();
}

async function continuePastNetworkSecurity(): Promise<void> {
  await waitFor(() => expect(screen.getByText(/Network Security/i)).toBeTruthy());
  const button =
    screen.queryByRole("button", { name: /^Skip$/i }) ??
    screen.getByRole("button", { name: /^Continue$/i });
  fireEvent.click(button);
  await flush();
}

async function flush(ms = 0): Promise<void> {
  await act(async () => {
    if (ms > 0) await new Promise((resolve) => setTimeout(resolve, ms));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("setup wizard end-to-end", () => {
  beforeEach(() => {
    process.env.GOBBY_SKIP_BOOTSTRAP = "1";
    mocks.checkHealth.mockReset();
    mocks.detectAllTools.mockReset();
    mocks.exec.mockReset();
    mocks.exec.mockImplementation((_command, _options, callback) => {
      callback?.(null);
      return { kill: vi.fn() };
    });
    mocks.execSync.mockReset();
    mocks.existsSync.mockReset();
    mocks.findRepos.mockReset();
    mocks.isGobbyInstalled.mockReset();
    mocks.lstatSync.mockReset();
    mocks.loadState.mockReset();
    mocks.mkdirSync.mockReset();
    mocks.patchPorts.mockReset();
    mocks.runGobby.mockReset();
    mocks.saveState.mockReset();
    mocks.spawnSync.mockReset();
    mocks.symlinkSync.mockReset();
    mocks.unlinkSync.mockReset();
    mocks.writeFileSync.mockReset();

    mocks.checkHealth.mockResolvedValue(true);
    mocks.detectAllTools.mockReturnValue({
      detected: {
        python: true,
        node: true,
        uv: true,
        tmux: true,
        git: true,
        docker: true,
        tailscale: false,
      },
      versions: {
        python: "Python 3.13",
        node: "v24.0.0",
        uv: "uv 0.8.0",
        tmux: "tmux 3.5",
        git: "git 2.50.0",
        docker: "Docker 29.4.3",
      },
    });
    mocks.existsSync.mockReturnValue(false);
    mocks.findRepos.mockResolvedValue([]);
    mocks.isGobbyInstalled.mockReturnValue(true);
    mocks.loadState.mockReturnValue(createState());
    mocks.runGobby.mockReturnValue({ success: true, output: "" });
    mocks.spawnSync.mockReturnValue({ status: 0 });
  });

  afterEach(() => {
    delete process.env.GOBBY_SKIP_BOOTSTRAP;
  });

  it("completes a cold FalkorDB install path with a custom password and launches", async () => {
    render(<App />);

    await click(/Let's go!/i);
    await submit("Ada");
    await flush(300);

    await click(/Continue/i);
    await click(/No, use defaults/i);
    await flush(300);

    await continuePastNetworkSecurity();
    await flush(300);

    await waitFor(() => expect(screen.getByText(/No uninitialized git repositories/i)).toBeTruthy());
    await click(/Continue/i);
    await flush(300);

    await waitFor(() => expect(screen.getByText(/No AI coding CLIs detected/i)).toBeTruthy());
    await flush(350);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Skip all remaining/i })).toBeTruthy(),
    );
    await click(/Skip all remaining/i);
    await flush(300);

    await waitFor(() =>
      expect(screen.getByText(/Install FalkorDB knowledge graph/i)).toBeTruthy(),
    );
    await submit("p");
    await waitFor(() => expect(screen.getByText(/Enter FalkorDB password/i)).toBeTruthy());
    await submit("ValidPassword123!");
    await flush(300);

    await waitFor(() => expect(screen.getByText(/Create a shortcut on your Desktop/i)).toBeTruthy());
    await click(/^No$/i);
    await flush(300);

    await waitFor(() => expect(screen.getByText(/Setup complete!/i)).toBeTruthy());

    expect(mocks.runGobby).toHaveBeenCalledWith(["install"], { timeout: 30000 });
    expect(mocks.runGobby).toHaveBeenCalledWith(
      ["install", "--falkordb", "--falkordb-password", "ValidPassword123!"],
      { timeout: 120000 },
    );
    expect(mocks.runGobby).toHaveBeenCalledWith(["init", "--name", "_personal"], {
      cwd: "/tmp/gobby-home/personal",
      timeout: 15000,
    });
    expect(mocks.runGobby).toHaveBeenCalledWith(["start"], { timeout: 15000 });
    expect(mocks.checkHealth).toHaveBeenCalledWith(60887, 30000);

    const launchState = mocks.saveState.mock.calls
      .map(([state]) => state as SetupState)
      .find((state) => state.completed_step_id === "launch");
    expect(launchState).toMatchObject({
      user_name: "Ada",
      falkordb_installed: true,
      falkordb_password_set: true,
      personal_dir_created: true,
      completed_step_id: "launch",
    });
    expect(launchState).not.toHaveProperty("neo4j_installed");
    expect(launchState).not.toHaveProperty("neo4j_password_set");

    const summary = mocks.writeFileSync.mock.calls[0]?.[1] as string;
    expect(summary).toContain("- FalkorDB: installed (Docker)");
    expect(summary).toContain("- FalkorDB password: custom");
    expect(summary).not.toContain("Neo4j");
  }, 15000);
});
