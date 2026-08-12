import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { WorkflowVariablesEditor } from "../WorkflowVariablesEditor";
import { parseVariableInput, variableDisplayValue } from "../workflowVariables";
import type { WorkflowDetail } from "../../../hooks/useWorkflows";

const mocks = vi.hoisted(() => ({
  workflows: [] as WorkflowDetail[],
  isLoading: false,
  fetchWorkflows: vi.fn(async (_params?: { workflow_type?: string }) => true),
  createWorkflow: vi.fn(async (_params: Record<string, unknown>) => null),
  toggleEnabled: vi.fn(
    async (_id: string): Promise<WorkflowDetail | null> => null,
  ),
  deleteWorkflow: vi.fn(async (_id: string) => true),
}));

vi.mock("../../../hooks/useWorkflows", () => ({
  useWorkflows: () => ({
    workflows: mocks.workflows,
    isLoading: mocks.isLoading,
    fetchWorkflows: mocks.fetchWorkflows,
    createWorkflow: mocks.createWorkflow,
    toggleEnabled: mocks.toggleEnabled,
    deleteWorkflow: mocks.deleteWorkflow,
  }),
}));

function makeVariable(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    id: "var-1",
    name: "max_retries",
    description: "Default retry budget",
    workflow_type: "variable",
    version: "1.0",
    enabled: true,
    priority: 100,
    source: "installed",
    sources: null,
    tags: null,
    project_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    deleted_at: null,
    definition_json: JSON.stringify({ variable: "max_retries", value: 3 }),
    canvas_json: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.workflows = [];
  mocks.isLoading = false;
  mocks.fetchWorkflows.mockReset().mockResolvedValue(true);
  mocks.createWorkflow.mockReset().mockResolvedValue(null);
  mocks.toggleEnabled.mockReset().mockResolvedValue(null);
  mocks.deleteWorkflow.mockReset().mockResolvedValue(true);
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
  it("formats stored definition values and tolerates malformed JSON", () => {
    expect(variableDisplayValue(JSON.stringify({ value: true }))).toBe("true");
    expect(variableDisplayValue(JSON.stringify({ value: [1, 2] }))).toBe(
      "[1,2]",
    );
    expect(variableDisplayValue(JSON.stringify({ value: "x" }))).toBe("x");
    expect(variableDisplayValue(JSON.stringify({ value: null }))).toBe("null");
    expect(variableDisplayValue("not json")).toBe("-");
  });
});

describe("WorkflowVariablesEditor", () => {
  it("scopes its initial fetch to variable definitions", () => {
    render(<WorkflowVariablesEditor />);
    expect(mocks.fetchWorkflows).toHaveBeenCalledWith({
      workflow_type: "variable",
    });
  });

  it("renders an empty state when there are no variable definitions", () => {
    mocks.workflows = [];
    render(<WorkflowVariablesEditor />);
    expect(screen.getByText(/No variable defaults yet/i)).toBeInTheDocument();
  });

  it("renders a row per variable with its name, display value, source, and enabled state", () => {
    mocks.workflows = [makeVariable()];
    render(<WorkflowVariablesEditor />);

    expect(screen.getByText("max_retries")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("installed")).toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: "Toggle max_retries" }),
    ).toBeChecked();
  });

  it("only renders variable-typed definitions, ignoring other workflow rows", () => {
    mocks.workflows = [
      makeVariable(),
      makeVariable({
        id: "wf-1",
        name: "some_pipeline",
        workflow_type: "pipeline",
      }),
    ];
    render(<WorkflowVariablesEditor />);

    expect(screen.getByText("max_retries")).toBeInTheDocument();
    expect(screen.queryByText("some_pipeline")).toBeNull();
  });

  it("creates a variable with a parsed default value from the add form", async () => {
    render(<WorkflowVariablesEditor />);

    fireEvent.click(screen.getByRole("button", { name: "Add variable" }));
    fireEvent.change(screen.getByLabelText("Variable name"), {
      target: { value: "feature_flag" },
    });
    fireEvent.change(screen.getByLabelText("Default value"), {
      target: { value: "true" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save variable" }));

    await waitFor(() => expect(mocks.createWorkflow).toHaveBeenCalledTimes(1));
    const arg = mocks.createWorkflow.mock.calls[0]?.[0];
    expect(arg?.name).toBe("feature_flag");
    expect(arg?.workflow_type).toBe("variable");
    expect(arg?.enabled).toBe(true);
    expect(JSON.parse(String(arg?.definition_json))).toMatchObject({
      variable: "feature_flag",
      value: true,
    });
  });

  it("does not submit when the name is blank", () => {
    render(<WorkflowVariablesEditor />);
    fireEvent.click(screen.getByRole("button", { name: "Add variable" }));
    fireEvent.click(screen.getByRole("button", { name: "Save variable" }));
    expect(mocks.createWorkflow).not.toHaveBeenCalled();
  });

  it("toggles a variable through the hook", async () => {
    mocks.workflows = [makeVariable()];
    mocks.toggleEnabled.mockResolvedValue(makeVariable({ enabled: false }));
    const user = userEvent.setup();
    render(<WorkflowVariablesEditor />);

    await user.click(
      screen.getByRole("switch", { name: "Toggle max_retries" }),
    );
    await waitFor(() =>
      expect(mocks.toggleEnabled).toHaveBeenCalledWith("var-1"),
    );
  });

  it("deletes a deletable variable after confirmation", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mocks.workflows = [makeVariable()];
    render(<WorkflowVariablesEditor />);

    fireEvent.click(screen.getByRole("button", { name: "Delete max_retries" }));
    expect(mocks.deleteWorkflow).toHaveBeenCalledWith("var-1");
  });

  it("does not offer deletion for bundled template variables", () => {
    mocks.workflows = [makeVariable({ source: "template" })];
    render(<WorkflowVariablesEditor />);
    expect(
      screen.queryByRole("button", { name: "Delete max_retries" }),
    ).toBeNull();
  });

  it("distinguishes a failed fetch from an empty variable list", async () => {
    mocks.fetchWorkflows.mockResolvedValue(false);

    render(<WorkflowVariablesEditor />);

    expect(await screen.findByRole("alert", { name: "" })).toHaveTextContent(
      "Could not load variable defaults.",
    );
    expect(screen.queryByText("No variable defaults yet.")).toBeNull();
  });

  it("shows a save failure and keeps the add form open", async () => {
    render(<WorkflowVariablesEditor />);
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
    mocks.workflows = [makeVariable()];
    mocks.deleteWorkflow.mockResolvedValue(false);
    render(<WorkflowVariablesEditor />);

    fireEvent.click(screen.getByRole("button", { name: "Delete max_retries" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      'Could not delete "max_retries".',
    );
  });
});
