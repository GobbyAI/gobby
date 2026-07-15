import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../../utils/state.js";
import { CliHooks } from "../CliHooks.js";
import { PersonalWorkspace } from "../PersonalWorkspace.js";
import { ProjectDiscovery } from "../ProjectDiscovery.js";

const mocks = vi.hoisted(() => ({
  findRepos: vi.fn(),
  mkdirSync: vi.fn(),
  runGobby: vi.fn(),
  saveState: vi.fn(),
}));

vi.mock("fs", () => ({
  default: {
    existsSync: () => false,
    lstatSync: vi.fn(),
    mkdirSync: mocks.mkdirSync,
    symlinkSync: vi.fn(),
    unlinkSync: vi.fn(),
  },
  existsSync: vi.fn(() => false),
  lstatSync: vi.fn(),
  mkdirSync: mocks.mkdirSync,
  symlinkSync: vi.fn(),
  unlinkSync: vi.fn(),
}));

vi.mock("ink", () => ({
  Box: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children?: React.ReactNode }) => <span>{children}</span>,
}));

vi.mock("ink-spinner", () => ({ default: () => <span data-testid="spinner" /> }));

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

vi.mock("../../components/MultiSelect.js", () => ({
  MultiSelect: ({
    items,
    onSubmit,
  }: {
    items: Array<{ value: string }>;
    onSubmit: (values: string[]) => void;
  }) => (
    <button onClick={() => onSubmit(items.map((item) => item.value))}>
      Initialize selected
    </button>
  ),
}));

vi.mock("../../utils/gobby.js", () => ({ runGobby: mocks.runGobby }));
vi.mock("../../utils/repos.js", () => ({
  displayPath: (value: string) => value,
  findRepos: mocks.findRepos,
}));
vi.mock("../../utils/state.js", () => ({
  getGobbyHome: () => "/tmp/gobby-home",
  saveState: mocks.saveState,
}));

function createState(overrides: Partial<SetupState> = {}): SetupState {
  return {
    completed_step_id: null,
    detected_tools: {},
    installed_clis: [],
    ports: { http: 60887, ws: 60888, ui: 60889 },
    projects: [],
    secrets_configured: [],
    ...overrides,
  } as unknown as SetupState;
}

function renderStep(component: React.ReactElement) {
  const onNext = vi.fn();
  let state = createState();
  const setState = vi.fn((updater: (prev: SetupState) => SetupState) => {
    state = updater(state);
  });
  render(React.cloneElement(component, { setState, onNext }));
  return { onNext, setState, getState: () => state };
}

describe("setup command failure states", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.findRepos.mockReset();
    mocks.mkdirSync.mockReset();
    mocks.runGobby.mockReset();
    mocks.saveState.mockReset();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("persists only successfully initialized projects and waits after failures", async () => {
    mocks.findRepos.mockResolvedValue(["/repos/good", "/repos/bad"]);
    mocks.runGobby
      .mockReturnValueOnce({ success: true, output: "" })
      .mockReturnValueOnce({ success: false, output: "init rejected" });
    const view = renderStep(
      <ProjectDiscovery state={createState()} setState={vi.fn()} onNext={vi.fn()} />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Initialize selected" }));

    expect(await screen.findByText(/1 project initialization failed/i)).toBeTruthy();
    expect(view.setState).not.toHaveBeenCalled();
    expect(view.onNext).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Continue without failed projects" }));

    expect(view.getState().projects).toEqual(["/repos/good"]);
  });

  it("keeps personal workspace initialization failures incomplete", async () => {
    mocks.runGobby.mockReturnValue({ success: false, output: "database unavailable" });
    const view = renderStep(
      <PersonalWorkspace state={createState()} setState={vi.fn()} onNext={vi.fn()} />,
    );

    expect(await screen.findByText(/Personal workspace failed: database unavailable/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(view.setState).not.toHaveBeenCalled();
    expect(view.onNext).not.toHaveBeenCalled();
  });

  it("keeps hook installation failures visible until the operator acts", async () => {
    mocks.runGobby.mockReturnValue({ success: false, output: "hook install failed" });
    const state = createState({ detected_tools: { claude: true } });
    const view = renderStep(<CliHooks state={state} setState={vi.fn()} onNext={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Initialize selected" }));

    expect(await screen.findByText(/Hook installation failed: hook install failed/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(view.setState).not.toHaveBeenCalled();
    expect(view.onNext).not.toHaveBeenCalled();
  });
});
