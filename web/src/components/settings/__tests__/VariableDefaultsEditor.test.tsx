import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { VariableDefaultsEditor } from "../VariableDefaultsEditor";
import { parseVariableInput, variableDisplayValue } from "../workflowVariables";
import type { VariableDef } from "../../../hooks/useVariableDefs";

const mocks = vi.hoisted(() => ({
  variables: [] as VariableDef[],
  isLoading: false,
  fetchVariables: vi.fn(async () => true),
  createVariable: vi.fn(async (_params: Record<string, unknown>) => null),
  toggleEnabled: vi.fn(
    async (_id: string): Promise<VariableDef | null> => null,
  ),
  deleteVariable: vi.fn(async (_id: string) => true),
}));

vi.mock("../../../hooks/useVariableDefs", () => ({
  useVariableDefs: () => ({
    variables: mocks.variables,
    isLoading: mocks.isLoading,
    fetchVariables: mocks.fetchVariables,
    createVariable: mocks.createVariable,
    toggleEnabled: mocks.toggleEnabled,
    deleteVariable: mocks.deleteVariable,
  }),
}));

function makeVariable(overrides: Partial<VariableDef> = {}): VariableDef {
  return {
    id: "var-1",
    name: "max_retries",
    description: "Default retry budget",
    enabled: true,
    source: "installed",
    tags: null,
    project_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    deleted_at: null,
    default_value: 3,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.variables = [];
  mocks.isLoading = false;
  mocks.fetchVariables.mockReset().mockResolvedValue(true);
  mocks.createVariable.mockReset().mockResolvedValue(null);
  mocks.toggleEnabled.mockReset().mockResolvedValue(null);
  mocks.deleteVariable.mockReset().mockResolvedValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("parseVariableInput", () => {
  it("coerces JSON-ish literals and falls back to the raw string", () => {
    expect(parseVariableInput("true")).toBe(true);
    expect(parseVariableInput("false")).toBe(false);
    expect(parseVariableInput("null")).toBeNull();
    expect(parseVariableInput("[]")).toEqual([]);
    expect(parseVariableInput("42")).toBe(42);
    expect(parseVariableInput("-7")).toBe(-7);
    expect(parseVariableInput("3.14")).toBeCloseTo(3.14);
    expect(parseVariableInput("hello")).toBe("hello");
  });
});

describe("variableDisplayValue", () => {
  it("formats default_value and treats missing as null", () => {
    expect(variableDisplayValue(true)).toBe("true");
    expect(variableDisplayValue([1, 2])).toBe("[1,2]");
    expect(variableDisplayValue("x")).toBe("x");
    expect(variableDisplayValue(null)).toBe("null");
    expect(variableDisplayValue(undefined)).toBe("null");
  });
});

describe("VariableDefaultsEditor", () => {
  it("loads variable defaults from the domain hook", () => {
    render(<VariableDefaultsEditor />);
    expect(mocks.fetchVariables).toHaveBeenCalled();
  });

  it("renders an empty state when there are no variable definitions", () => {
    mocks.variables = [];
    render(<VariableDefaultsEditor />);
    expect(screen.getByText(/No variable defaults yet/i)).toBeInTheDocument();
  });

  it("renders a row per variable with its name, display value, source, and enabled state", () => {
    mocks.variables = [makeVariable()];
    render(<VariableDefaultsEditor />);

    expect(screen.getByText("max_retries")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("installed")).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "Toggle max_retries" }),
    ).toBeChecked();
  });

  it("creates a variable with a parsed default value from the add form", async () => {
    render(<VariableDefaultsEditor />);

    fireEvent.click(screen.getByRole("button", { name: "Add variable" }));
    fireEvent.change(screen.getByLabelText("Variable name"), {
      target: { value: "feature_flag" },
    });
    fireEvent.change(screen.getByLabelText("Default value"), {
      target: { value: "true" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save variable" }));

    await waitFor(() => expect(mocks.createVariable).toHaveBeenCalledTimes(1));
    const arg = mocks.createVariable.mock.calls[0]?.[0];
    expect(arg?.name).toBe("feature_flag");
    expect(arg?.value).toBe(true);
    expect(arg?.enabled).toBe(true);
    expect(arg).not.toHaveProperty(["workflow", "type"].join("_"));
    expect(arg).not.toHaveProperty("definition_json");
  });

  it("does not submit when the name is blank", () => {
    render(<VariableDefaultsEditor />);
    fireEvent.click(screen.getByRole("button", { name: "Add variable" }));
    fireEvent.click(screen.getByRole("button", { name: "Save variable" }));
    expect(mocks.createVariable).not.toHaveBeenCalled();
  });

  it("toggles a variable through the hook", async () => {
    mocks.variables = [makeVariable()];
    mocks.toggleEnabled.mockResolvedValue(makeVariable({ enabled: false }));
    const user = userEvent.setup();
    render(<VariableDefaultsEditor />);

    await user.click(
      screen.getByRole("switch", { name: "Toggle max_retries" }),
    );
    await waitFor(() =>
      expect(mocks.toggleEnabled).toHaveBeenCalledWith("var-1"),
    );
  });

  it("deletes a deletable variable after confirmation", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mocks.variables = [makeVariable()];
    render(<VariableDefaultsEditor />);

    fireEvent.click(screen.getByRole("button", { name: "Delete max_retries" }));
    expect(mocks.deleteVariable).toHaveBeenCalledWith("var-1");
  });

  it("does not offer deletion for bundled template variables", () => {
    mocks.variables = [makeVariable({ source: "template" })];
    render(<VariableDefaultsEditor />);
    expect(
      screen.queryByRole("button", { name: "Delete max_retries" }),
    ).toBeNull();
  });

  it("does not render an enabled switch for bundled template variables", () => {
    mocks.variables = [makeVariable({ source: "template", enabled: true })];
    render(<VariableDefaultsEditor />);
    expect(
      screen.queryByRole("switch", { name: "Toggle max_retries" }),
    ).toBeNull();
  });

  it("distinguishes a failed fetch from an empty variable list", async () => {
    mocks.fetchVariables.mockResolvedValue(false);

    render(<VariableDefaultsEditor />);

    expect(await screen.findByRole("alert", { name: "" })).toHaveTextContent(
      "Could not load variable defaults.",
    );
    expect(screen.queryByText("No variable defaults yet.")).toBeNull();
  });

  it("shows a save failure and keeps the add form open", async () => {
    render(<VariableDefaultsEditor />);
    fireEvent.click(screen.getByRole("button", { name: "Add variable" }));
    fireEvent.change(screen.getByLabelText("Variable name"), {
      target: { value: "feature_flag" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save variable" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save the variable.",
    );
    expect(screen.getByLabelText("Variable name")).toHaveValue("feature_flag");
  });

  it("shows a delete failure", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mocks.variables = [makeVariable()];
    mocks.deleteVariable.mockResolvedValue(false);
    render(<VariableDefaultsEditor />);

    fireEvent.click(screen.getByRole("button", { name: "Delete max_retries" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      'Could not delete "max_retries".',
    );
  });
});
