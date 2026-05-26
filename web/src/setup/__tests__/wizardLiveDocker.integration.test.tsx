import React from "react";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { dirname, join, resolve } from "path";
import { spawnSync } from "child_process";
import type { SpawnSyncReturns } from "child_process";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App.js";

const runLiveDocker = process.env.GOBBY_LIVE_FALKORDB_SETUP === "1";
const describeLive = runLiveDocker ? describe : describe.skip;

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

vi.mock("../utils/detect.js", () => ({
  OPTIONAL_TOOLS: ["claude", "gemini", "qwen", "codex", "droid", "tailscale"],
  REQUIRED_TOOLS: ["python", "node", "uv", "tmux", "git", "docker"],
  detectAllTools: () => ({
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
      node: process.version,
      uv: "uv",
      tmux: "tmux",
      git: "git",
      docker: "docker",
    },
  }),
  detectTool: () => "present",
}));

vi.mock("../utils/repos.js", () => ({
  displayPath: (path: string) => path,
  findRepos: () => Promise.resolve([]),
}));

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

async function flush(ms = 0): Promise<void> {
  await act(async () => {
    if (ms > 0) await new Promise((resolve) => setTimeout(resolve, ms));
    await Promise.resolve();
    await Promise.resolve();
  });
}

function run(
  command: string,
  args: string[],
  env: NodeJS.ProcessEnv,
  timeout = 120000,
): SpawnSyncReturns<string> {
  const result = spawnSync(command, args, {
    cwd: repoRoot(),
    encoding: "utf-8",
    env,
    timeout,
  });
  return result as SpawnSyncReturns<string>;
}

function repoRoot(): string {
  return resolve(process.cwd(), "..");
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function createGobbyWrapper(gobbyHome: string): string {
  const binDir = join(gobbyHome, "bin");
  const wrapper = join(binDir, "gobby");
  mkdirSync(binDir, { recursive: true });
  writeFileSync(
    wrapper,
    `#!/bin/sh\ncd ${shellQuote(repoRoot())} && exec uv run gobby "$@"\n`,
    { mode: 0o755 },
  );
  chmodSync(wrapper, 0o755);
  return wrapper;
}

async function expectBrowserLoads(): Promise<void> {
  const response = await fetch("http://localhost:13000", {
    signal: AbortSignal.timeout(10000),
  });
  expect(response.ok).toBe(true);
}

describeLive("setup wizard live Docker integration", () => {
  let gobbyHome: string;
  let originalEnv: NodeJS.ProcessEnv;
  let liveEnv: NodeJS.ProcessEnv;

  beforeEach(() => {
    originalEnv = { ...process.env };
    gobbyHome = mkdtempSync(join(tmpdir(), "gobby-live-wizard-"));
    const wrapper = createGobbyWrapper(gobbyHome);
    liveEnv = {
      ...process.env,
      GOBBY_HOME: gobbyHome,
      GOBBY_BIN: wrapper,
      GOBBY_SKIP_BOOTSTRAP: "1",
      PATH: `${dirname(wrapper)}:${process.env.PATH ?? ""}`,
    };
    Object.assign(process.env, liveEnv);

    const docker = run("docker", ["version"], liveEnv, 10000);
    expect(docker.status, docker.stderr || docker.stdout).toBe(0);
  });

  afterEach(() => {
    if (liveEnv) {
      run(liveEnv.GOBBY_BIN ?? "gobby", ["uninstall", "--falkordb"], liveEnv, 90000);
      run(liveEnv.GOBBY_BIN ?? "gobby", ["stop"], liveEnv, 30000);
    }
    process.env = originalEnv;
    if (gobbyHome) rmSync(gobbyHome, { recursive: true, force: true });
  });

  it(
    "completes a cold custom-password FalkorDB path against Docker",
    async () => {
      render(<App />);

      await click(/Let's go!/i);
      await submit("Ada");
      await flush(300);

      await click(/Continue/i);
      await click(/No, use defaults/i);
      await flush(300);

      await click(/Skip/i);
      await flush(300);

      await waitFor(() => expect(screen.getByText(/No uninitialized git repositories/i)).toBeTruthy());
      await click(/Continue/i);
      await flush(300);

      await waitFor(() => expect(screen.getByText(/No AI coding CLIs detected/i)).toBeTruthy());
      await flush(350);

      await waitFor(() =>
        expect(screen.getByText(/Install FalkorDB knowledge graph/i)).toBeTruthy(),
      );
      await submit("p");
      await waitFor(() => expect(screen.getByText(/Enter FalkorDB password/i)).toBeTruthy());
      await submit("ValidPassword123!");
      await flush(300);

      await waitFor(() => expect(screen.getByText(/Create a shortcut on your Desktop/i)).toBeTruthy(), {
        timeout: 150000,
      });
      await click(/^No$/i);
      await flush(300);

      await waitFor(() => expect(screen.getByText(/Setup complete!/i)).toBeTruthy(), {
        timeout: 60000,
      });

      const composeFile = join(gobbyHome, "services", "docker-compose.yml");
      const ps = run(
        "docker",
        ["compose", "-f", composeFile, "ps"],
        liveEnv,
        30000,
      );
      expect(ps.status, ps.stderr || ps.stdout).toBe(0);
      expect(ps.stdout).toContain("falkordb");
      expect(ps.stdout.toLowerCase()).toContain("healthy");

      await expectBrowserLoads();

      const status = run(liveEnv.GOBBY_BIN ?? "gobby", ["status"], liveEnv, 30000);
      expect(status.status, status.stderr || status.stdout).toBe(0);
      expect(status.stdout).toMatch(/FalkorDB/i);
      expect(status.stdout).toMatch(/healthy/i);

      const state = JSON.parse(
        readFileSync(join(gobbyHome, "setup_state.json"), "utf-8"),
      ) as Record<string, unknown>;
      expect(state).toMatchObject({
        falkordb_installed: true,
        falkordb_password_set: true,
        personal_dir_created: true,
        completed_step_id: "launch",
      });
      expect(state).not.toHaveProperty("neo4j_installed");
      expect(state).not.toHaveProperty("neo4j_password_set");
    },
    240000,
  );

  it("migrates legacy Neo4j setup state without crashing", () => {
    writeFileSync(
      join(gobbyHome, "setup_state.json"),
      JSON.stringify({
        version: 2,
        started_at: "2026-05-22T00:00:00.000Z",
        completed_at: null,
        completed_step_id: "services",
        neo4j_installed: true,
        neo4j_password_set: false,
      }),
    );

    const result = run(
      "npm",
      [
        "--prefix",
        "web",
        "exec",
        "tsx",
        "--",
        "--eval",
        "import { loadState } from './web/src/setup/utils/state.ts'; loadState();",
      ],
      liveEnv,
      30000,
    );
    expect(result.status, result.stderr || result.stdout).toBe(0);

    const migrated = JSON.parse(
      readFileSync(join(gobbyHome, "setup_state.json"), "utf-8"),
    ) as Record<string, unknown>;
    expect(migrated).toMatchObject({
      version: 3,
      falkordb_installed: false,
      falkordb_password_set: false,
    });
    expect(migrated).not.toHaveProperty("neo4j_installed");
    expect(migrated).not.toHaveProperty("neo4j_password_set");
  });
});
