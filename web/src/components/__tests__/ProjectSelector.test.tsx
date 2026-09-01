import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ProjectSelector } from "../ProjectSelector";
import type { ProjectOption } from "../../types/chat";

const PROJECTS: ProjectOption[] = [
  { id: "personal", name: "Personal" },
  { id: "project-gobby", name: "gobby" },
  { id: "project-demo", name: "demo" },
];

function renderSelector(
  selectedProjectId: string | null = "project-gobby",
  onProjectChange = vi.fn(),
) {
  render(
    <ProjectSelector
      projects={PROJECTS}
      selectedProjectId={selectedProjectId}
      onProjectChange={onProjectChange}
    />,
  );
  return { onProjectChange };
}

describe("ProjectSelector", () => {
  it("keeps the desktop segmented project scope control", () => {
    renderSelector();

    const group = screen.getByRole("radiogroup", { name: "Project scope" });
    expect(
      within(group).getByRole("radio", { name: "Personal" }),
    ).toBeInTheDocument();
    expect(
      within(group).getByRole("radio", { name: "gobby" }),
    ).toBeInTheDocument();
  });

  it("keeps segmented options and the compact trigger dense with invisible hit areas (#19181)", () => {
    renderSelector();

    // The control keeps its 1.75rem header height on touch; each option's
    // coarseHitAreaCls ::before floors the tap target at 44×44 instead of
    // inflating the rendered box.
    const group = screen.getByRole("radiogroup", { name: "Project scope" });
    expect(group).not.toHaveClass("pointer-coarse:min-h-11", "overflow-hidden");
    for (const radio of within(group).getAllByRole("radio")) {
      expect(radio).toHaveClass(
        "relative",
        "pointer-coarse:before:min-h-11",
        "pointer-coarse:before:min-w-11",
      );
      expect(radio).not.toHaveClass(
        "pointer-coarse:min-h-11",
        "pointer-coarse:min-w-11",
      );
    }

    const compactTrigger = screen.getByRole("button", {
      name: "Project scope: gobby",
    });
    expect(compactTrigger).toHaveClass(
      "min-h-7",
      "pointer-coarse:before:min-h-11",
    );
    expect(compactTrigger).not.toHaveClass(
      "pointer-coarse:min-h-11",
      "pointer-coarse:min-w-11",
    );
  });

  it("opens the one-item mobile project selector with Personal in the list", () => {
    const { onProjectChange } = renderSelector();

    fireEvent.click(
      screen.getByRole("button", { name: "Project scope: gobby" }),
    );

    const listbox = screen.getByRole("listbox", {
      name: "Project scope options",
    });
    expect(
      within(listbox).getByRole("option", { name: "Personal" }),
    ).toBeInTheDocument();
    expect(
      within(listbox).getByRole("option", { name: "gobby" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      within(listbox).getByRole("option", { name: "gobby" }),
    ).toHaveAttribute("tabindex", "-1");

    fireEvent.click(within(listbox).getByRole("option", { name: "Personal" }));
    expect(onProjectChange).toHaveBeenCalledWith("personal");
  });

  it("links project search combobox ARIA to the highlighted option", async () => {
    const { onProjectChange } = renderSelector();
    const group = screen.getByRole("radiogroup", { name: "Project scope" });

    fireEvent.click(within(group).getByRole("radio", { name: "gobby" }));

    const input = screen.getByRole("combobox");
    const listbox = screen.getByRole("listbox", {
      name: "Project search results",
    });
    const gobbyOption = within(listbox).getByRole("option", { name: "gobby" });
    const demoOption = within(listbox).getByRole("option", { name: "demo" });

    expect(input).toHaveAttribute("aria-controls", listbox.id);
    expect(input).toHaveAttribute("aria-activedescendant", gobbyOption.id);

    fireEvent.keyDown(input, { key: "ArrowDown" });

    expect(input).toHaveAttribute("aria-activedescendant", demoOption.id);

    fireEvent.keyDown(input, { key: "Enter" });

    expect(onProjectChange).toHaveBeenCalledWith("project-demo");
    await waitFor(() =>
      expect(within(group).getByRole("radio", { name: "gobby" })).toHaveFocus(),
    );
  });

  it("restores segmented control focus when Escape closes search", async () => {
    renderSelector();
    const group = screen.getByRole("radiogroup", { name: "Project scope" });
    const projectRadio = within(group).getByRole("radio", { name: "gobby" });

    fireEvent.click(projectRadio);

    const input = screen.getByRole("combobox");
    fireEvent.keyDown(input, { key: "Escape" });

    expect(
      screen.queryByRole("listbox", { name: "Project search results" }),
    ).toBeNull();
    await waitFor(() => expect(projectRadio).toHaveFocus());
  });

  it("toggles the compact selector from keyboard and restores focus on Escape", async () => {
    renderSelector();
    const trigger = screen.getByRole("button", {
      name: "Project scope: gobby",
    });

    fireEvent.keyDown(trigger, { key: "Enter" });

    const listbox = screen.getByRole("listbox", {
      name: "Project scope options",
    });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", listbox.id);

    fireEvent.keyDown(listbox, { key: "Escape" });

    expect(
      screen.queryByRole("listbox", { name: "Project scope options" }),
    ).toBeNull();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("selects from the compact selector with arrow keys and Enter", async () => {
    const { onProjectChange } = renderSelector("personal");

    fireEvent.click(
      screen.getByRole("button", { name: "Project scope: Personal" }),
    );

    const listbox = screen.getByRole("listbox", {
      name: "Project scope options",
    });
    const gobbyOption = within(listbox).getByRole("option", { name: "gobby" });
    await waitFor(() => expect(listbox).toHaveFocus());

    fireEvent.keyDown(listbox, { key: "ArrowDown" });
    expect(listbox).toHaveAttribute("aria-activedescendant", gobbyOption.id);

    fireEvent.keyDown(listbox, { key: "Enter" });

    expect(onProjectChange).toHaveBeenCalledWith("project-gobby");
  });

  it("keeps the empty-search notice outside the listbox", () => {
    renderSelector();
    const group = screen.getByRole("radiogroup", { name: "Project scope" });
    fireEvent.click(within(group).getByRole("radio", { name: "gobby" }));
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "zzz" },
    });

    const listbox = screen.getByRole("listbox", {
      name: "Project search results",
    });
    expect(within(listbox).queryAllByRole("option")).toHaveLength(0);
    expect(within(listbox).queryByText("No projects found")).toBeNull();
    expect(screen.getByText("No projects found")).toBeInTheDocument();
  });
});

describe("ProjectSelector without a checkout on this machine", () => {
  const PROJECTS_WITH_MISSING: ProjectOption[] = [
    { id: "personal", name: "Personal" },
    { id: "project-gobby", name: "gobby", hasCheckout: true },
    { id: "project-remote", name: "remote", hasCheckout: false },
  ];

  function renderMissing(selectedProjectId: string) {
    const onProjectChange = vi.fn();
    render(
      <ProjectSelector
        projects={PROJECTS_WITH_MISSING}
        selectedProjectId={selectedProjectId}
        onProjectChange={onProjectChange}
      />,
    );
    return { onProjectChange };
  }

  it("marks the project disabled in the compact listbox and refuses a click", () => {
    const { onProjectChange } = renderMissing("project-gobby");

    fireEvent.click(
      screen.getByRole("button", { name: "Project scope: gobby" }),
    );
    const listbox = screen.getByRole("listbox", {
      name: "Project scope options",
    });
    const remote = within(listbox).getByRole("option", { name: /remote/ });
    expect(remote).toHaveAttribute("aria-disabled", "true");
    expect(remote).toHaveTextContent("not checked out on this machine");
    expect(
      within(listbox).getByRole("option", { name: "gobby" }),
    ).not.toHaveAttribute("aria-disabled");

    fireEvent.click(remote);
    expect(onProjectChange).not.toHaveBeenCalled();
  });

  it("does not select the disabled project from the keyboard", async () => {
    const { onProjectChange } = renderMissing("personal");

    fireEvent.click(
      screen.getByRole("button", { name: "Project scope: Personal" }),
    );
    const listbox = screen.getByRole("listbox", {
      name: "Project scope options",
    });
    await waitFor(() => expect(listbox).toHaveFocus());

    fireEvent.keyDown(listbox, { key: "ArrowDown" });
    fireEvent.keyDown(listbox, { key: "ArrowDown" });
    expect(listbox).toHaveAttribute(
      "aria-activedescendant",
      within(listbox).getByRole("option", { name: /remote/ }).id,
    );

    fireEvent.keyDown(listbox, { key: "Enter" });
    expect(onProjectChange).not.toHaveBeenCalled();
  });

  it("opens the search picker instead of auto-selecting the only project when it is not checked out", () => {
    const onProjectChange = vi.fn();
    render(
      <ProjectSelector
        projects={[
          { id: "personal", name: "Personal" },
          { id: "project-remote", name: "remote", hasCheckout: false },
        ]}
        selectedProjectId="personal"
        onProjectChange={onProjectChange}
      />,
    );
    const group = screen.getByRole("radiogroup", { name: "Project scope" });

    fireEvent.click(within(group).getByRole("radio", { name: "Project" }));

    expect(onProjectChange).not.toHaveBeenCalled();
    const listbox = screen.getByRole("listbox", {
      name: "Project search results",
    });
    expect(
      within(listbox).getByRole("option", { name: /remote/ }),
    ).toHaveAttribute("aria-disabled", "true");
  });
});
