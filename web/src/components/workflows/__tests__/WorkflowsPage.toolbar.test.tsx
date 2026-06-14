import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkflowsPage } from "../WorkflowsPage";

describe("WorkflowsPage toolbar", () => {
  it("renders the Workflows shell without retired sub-tabs or pipeline actions", () => {
    render(<WorkflowsPage />);

    expect(screen.getByRole("heading", { name: "Workflows" })).toBeInTheDocument();
    expect(
      screen.getByText("Workflow definitions are managed from Activity."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pipelines" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rules" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Agents" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stages" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Profiles" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Filter workflows" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Pipeline" })).not.toBeInTheDocument();
  });
});
