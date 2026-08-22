import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentEditForm } from "../AgentEditForm";
import type { AgentFormData } from "../AgentEditForm.types";

const form: AgentFormData = {
  name: "reviewer",
  description: "",
  surfaces: ["spawn"],
  persona_prompt: "",
  agent_prompt: "Review the assigned implementation.",
  provider: "inherit",
  model: "",
  reasoning_effort: "auto",
  reasoning_required: false,
  mode: "default",
  isolation: "none",
  base_branch: "inherit",
  timeout: 0,
  pipeline: "",
  fallback_agent: "",
};

describe("AgentEditForm", () => {
  it("shows only prompt editors declared by the selected surfaces", () => {
    const { rerender } = render(
      <AgentEditForm
        isOpen
        form={form}
        onChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        isEditing
        providerCatalog={[]}
      />,
    );

    expect(screen.getByText("Agent prompt")).toBeInTheDocument();
    expect(screen.queryByText("Persona prompt")).not.toBeInTheDocument();

    rerender(
      <AgentEditForm
        isOpen
        form={{ ...form, surfaces: ["spawn", "persona"] }}
        onChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        isEditing
        providerCatalog={[]}
      />,
    );

    expect(screen.getByText("Agent prompt")).toBeInTheDocument();
    expect(screen.getByText("Persona prompt")).toBeInTheDocument();
  });

  it("renders the view picker as a roving tab list", () => {
    const onViewChange = vi.fn();

    render(
      <AgentEditForm
        isOpen
        form={form}
        onChange={vi.fn()}
        onSave={vi.fn()}
        onCancel={vi.fn()}
        isEditing
        providerCatalog={[]}
        sidebarView="form"
        onViewChange={onViewChange}
      />,
    );

    const tablist = screen.getByRole("tablist", { name: "Agent editor view" });
    const [formTab, yamlTab] = within(tablist).getAllByRole("tab");

    expect(formTab).toHaveAttribute("tabindex", "0");
    expect(yamlTab).toHaveAttribute("tabindex", "-1");

    formTab.focus();
    fireEvent.keyDown(formTab, { key: "ArrowRight" });
    expect(yamlTab).toHaveFocus();
    expect(onViewChange).toHaveBeenCalledWith("yaml");
  });
});
