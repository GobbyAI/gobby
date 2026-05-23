import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupState } from "../../utils/state.js";
import { Services } from "../Services.js";

const mocks = vi.hoisted(() => ({
  runGobby: vi.fn(),
  saveState: vi.fn(),
}));

vi.mock("ink", () => ({
  Box: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children?: React.ReactNode }) => <span>{children}</span>,
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
        const input = event.currentTarget as unknown as { value: string };
        onChange(input.value);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          const input = event.currentTarget as unknown as { value: string };
          onSubmit(input.value);
        }
      }}
    />
  ),
}));

vi.mock("../../utils/gobby.js", () => ({
  runGobby: mocks.runGobby,
}));

vi.mock("../../utils/state.js", () => ({
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
  } as unknown as SetupState;
}

function renderServices() {
  let state = createState();
  const onNext = vi.fn();
  const setState = vi.fn((updater: (prev: SetupState) => SetupState) => {
    state = updater(state);
  });

  render(<Services state={state} setState={setState} onNext={onNext} />);

  return {
    get state() {
      return state as unknown as Record<string, unknown>;
    },
    onNext,
    setState,
  };
}

function submit(value: string): void {
  const input = screen.getByLabelText("wizard input");
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter" });
}

function hasRenderedText(text: string): boolean {
  return (
    screen.queryAllByText((_, element) => element?.textContent?.includes(text) ?? false).length > 0
  );
}

describe("Services FalkorDB setup", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.runGobby.mockReset();
    mocks.runGobby.mockReturnValue({ success: true, output: "" });
    mocks.saveState.mockReset();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it.each(["has space", "control\u0007char", "nonascii-é"])(
    "rejects invalid FalkorDB password %s before invoking the CLI",
    (password) => {
      const view = renderServices();

      submit("p");
      expect(screen.getByText(/Enter .* password/i)).toBeTruthy();

      submit(password);

      expect(mocks.runGobby).not.toHaveBeenCalled();
      expect(view.setState).not.toHaveBeenCalled();
      expect(view.onNext).not.toHaveBeenCalled();
      expect(screen.getByText(/Enter FalkorDB password/i)).toBeTruthy();
      expect(screen.getByText(/FalkorDB password must/i)).toBeTruthy();
    },
  );

  it("rejects custom passwords with leading or trailing whitespace without trimming them", () => {
    const view = renderServices();

    submit("p");
    submit(" ValidPassword123!");

    expect(mocks.runGobby).not.toHaveBeenCalled();
    expect(view.setState).not.toHaveBeenCalled();
    expect(hasRenderedText("FalkorDB password must not contain whitespace")).toBe(true);
  });

  it("clears the password rejection when the operator edits the value", () => {
    renderServices();

    submit("p");
    submit("has space");
    expect(hasRenderedText("FalkorDB password must not contain whitespace")).toBe(true);

    fireEvent.change(screen.getByLabelText("wizard input"), { target: { value: "Valid123!" } });

    expect(hasRenderedText("FalkorDB password must not contain whitespace")).toBe(false);
  });

  it.each([
    ["has space", "FalkorDB password must not contain whitespace"],
    ["control\u0007char", "FalkorDB password must not contain ASCII control characters"],
    [
      "nonascii-é",
      "FalkorDB password must use printable ASCII only (Docker round-trip constraint)",
    ],
  ])(
    "surfaces the validator message for invalid FalkorDB password %s",
    (password, message) => {
      renderServices();

      submit("p");
      submit(password);

      expect(hasRenderedText(message)).toBe(true);
    },
  );

  it("installs FalkorDB with a valid password and records the renamed state fields", async () => {
    mocks.runGobby.mockReturnValue({ success: true, output: "installed" });
    const view = renderServices();

    submit("p");
    submit("ValidPassword123!");

    expect(view.setState).toHaveBeenCalledTimes(1);
    expect(mocks.runGobby).toHaveBeenCalledWith(
      ["install", "--falkordb", "--falkordb-password", "ValidPassword123!"],
      { timeout: 120000 },
    );
    expect(view.state).toMatchObject({
      falkordb_installed: true,
      falkordb_password_set: true,
      completed_step_id: "services",
    });
    expect(view.state).not.toHaveProperty("neo4j_installed");
    expect(view.state).not.toHaveProperty("neo4j_password_set");
  });

  it("returns to password entry when the FalkorDB CLI install rejects the password", () => {
    mocks.runGobby.mockReturnValue({
      success: false,
      output: "ValueError: FalkorDB password must not contain whitespace",
    });
    const view = renderServices();

    submit("p");
    submit("ValidPassword123!");

    expect(mocks.runGobby).toHaveBeenCalledTimes(1);
    expect(view.setState).not.toHaveBeenCalled();
    expect(view.onNext).not.toHaveBeenCalled();
    expect(screen.getByText(/Enter FalkorDB password/i)).toBeTruthy();
    expect(hasRenderedText("FalkorDB password must not contain whitespace")).toBe(true);
  });
});
