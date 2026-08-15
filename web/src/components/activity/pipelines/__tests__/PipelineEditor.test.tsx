import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PipelineDefDetail } from "../../../../hooks/usePipelineDefs";
import { PipelineEditor } from "../PipelineEditor";

vi.mock("../../../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn(async () => true),
    ConfirmDialogElement: null,
  }),
}));

const pipeline: PipelineDefDetail = {
  id: "pipeline-1",
  name: "deploy-prod",
  description: "Deploy production services",
  kind: "pipeline",
  version: "1.0",
  enabled: true,
  source: "installed",
  tags: ["release"],
  project_id: "project-1",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  deleted_at: null,
  definition_json: JSON.stringify({
    name: "deploy-prod",
    description: "Deploy production services",
    steps: [{ id: "test", exec: "npm test" }],
  }),
  canvas_json: null,
};

describe("PipelineEditor", () => {
  it("does not offer activate_workflow as a pipeline step type", async () => {
    const user = userEvent.setup();

    render(
      <PipelineEditor
        pipeline={pipeline}
        updateWorkflow={vi.fn()}
        onBack={vi.fn()}
        onExport={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /npm test/ }));

    const typeSelect = screen.getByRole("combobox", { name: "Type" });
    expect(typeSelect).not.toHaveTextContent("Workflow");
    expect(
      screen.queryByRole("option", { name: "Workflow" }),
    ).not.toBeInTheDocument();
  });

  it("shows a retryable error when the update returns no pipeline", async () => {
    const user = userEvent.setup();
    const updateWorkflow = vi.fn(async () => null);

    render(
      <PipelineEditor
        pipeline={pipeline}
        updateWorkflow={updateWorkflow}
        onBack={vi.fn()}
        onExport={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save the pipeline. Please try again.",
    );
    expect(updateWorkflow).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("shows the thrown update error and restores the save action", async () => {
    const user = userEvent.setup();
    const updateWorkflow = vi.fn(async () => {
      throw new Error("network unavailable");
    });

    render(
      <PipelineEditor
        pipeline={pipeline}
        updateWorkflow={updateWorkflow}
        onBack={vi.fn()}
        onExport={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Save failed: network unavailable",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});
