import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";

import { useIsMobile } from "../../../hooks/useIsMobile";
import { SessionsFilterDropdown } from "../SessionsFilterDropdown";
import {
  defaultSessionsFilters,
  type SessionsFilters,
} from "../sessionsFilters";

vi.mock("../../../hooks/useIsMobile", () => ({
  useIsMobile: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(useIsMobile).mockReturnValue(false);
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderDropdown(
  overrides: Partial<{
    filters: SessionsFilters;
    providerOptions: readonly string[];
  }> = {},
) {
  const onChange = vi.fn();
  const onClose = vi.fn();
  const providerOptions = overrides.providerOptions ?? [
    "claude",
    "codex",
    "unknown",
  ];
  const element = (filters: SessionsFilters) => (
    <SessionsFilterDropdown
      filters={filters}
      onChange={onChange}
      providerOptions={providerOptions}
      onClose={onClose}
    />
  );
  const view = render(element(overrides.filters ?? defaultSessionsFilters()));
  return {
    onChange,
    onClose,
    rerenderFilters: (filters: SessionsFilters) =>
      view.rerender(element(filters)),
    ...view,
  };
}

describe("SessionsFilterDropdown", () => {
  it("renders the filter section labels without model selection", () => {
    renderDropdown();
    expect(screen.getByText("Mode")).toBeInTheDocument();
    expect(screen.getByText("Provider")).toBeInTheDocument();
    expect(screen.queryByText("Model")).toBeNull();
    expect(screen.getByText("Session ref")).toBeInTheDocument();
    expect(screen.getByText("Task ref")).toBeInTheDocument();
    expect(screen.getByText("Date range")).toBeInTheDocument();
  });

  it("fits narrow panels: capped width, container-query column collapse, flexible ref inputs (#20045)", () => {
    renderDropdown();

    // The popover caps its own width to the tab content and sizes its body
    // with container queries so it never overflows a minimum-width panel.
    const panel = screen.getByRole("dialog", { name: "Session filters" });
    expect(panel.className).toContain("@container");
    expect(panel.className).toContain("max-w-[calc(100%-1rem)]");

    // Single column by default; two columns only when the popover has its
    // full 320px width.
    const grid = screen.getByText("Mode").closest(".grid")!;
    expect(grid.className).toContain("grid-cols-1");
    expect(grid.className).toContain(
      "@min-[20rem]:grid-cols-[auto_minmax(0,1fr)]",
    );

    // Date range lives in a full-width row so all preset segments stay visible.
    const dateColumn = screen.getByText("Date range").closest(".grid > div")!;
    expect(dateColumn.className).toContain("@min-[20rem]:col-span-2");
    expect(dateColumn.parentElement).toBe(grid);

    // From/to inputs flex with the column instead of clipping at fixed widths.
    for (const label of ["Session ref minimum", "Task ref maximum"]) {
      const wrapper = screen
        .getByLabelText(label, { selector: "input" })
        .closest("label")!;
      expect(wrapper.className).toContain("flex-1");
      expect(wrapper.className).not.toContain("shrink-0");
    }
  });

  it("renders default include-all modes as checked with the Autonomous label", () => {
    renderDropdown();
    expect(screen.getByLabelText("Interactive")).toBeChecked();
    expect(screen.getByLabelText("Autonomous")).toBeChecked();
    expect(screen.queryByLabelText("Auto")).toBeNull();
  });

  it("toggling a default Mode checkbox emits the remaining narrowed mode set", () => {
    const { onChange } = renderDropdown();
    fireEvent.click(screen.getByLabelText("Interactive"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect([...next.modes]).toEqual(["auto"]);
  });

  it("renders providers alphabetically with capitalized labels and checked by default", () => {
    renderDropdown({ providerOptions: ["unknown", "claude", "codex"] });

    const section = screen.getByText("Provider").parentElement!;
    expect(
      within(section)
        .getAllByRole("checkbox")
        .map((checkbox) => checkbox.nextSibling?.textContent),
    ).toEqual(["Claude", "Codex", "Unknown"]);
    expect(screen.getByLabelText("Claude")).toBeChecked();
    expect(screen.getByLabelText("Codex")).toBeChecked();
    expect(screen.getByLabelText("Unknown")).toBeChecked();
  });

  it("toggling a default Provider emits the remaining narrowed provider set", () => {
    const { onChange } = renderDropdown();
    fireEvent.click(screen.getByLabelText("Codex"));

    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect([...next.providers]).toEqual(["claude", "unknown"]);
  });

  it("toggling a default Provider uses the sorted provider option order", () => {
    const { onChange } = renderDropdown({
      providerOptions: ["unknown", "claude", "codex"],
    });
    fireEvent.click(screen.getByLabelText("Codex"));

    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect([...next.providers]).toEqual(["claude", "unknown"]);
  });

  it("typing a session ref bound emits onChange with the parsed integer", () => {
    const { onChange } = renderDropdown();
    fireEvent.change(
      screen.getByLabelText("Session ref minimum", { selector: "input" })!,
      {
        target: { value: "100" },
      },
    );
    expect(onChange).toHaveBeenCalledTimes(1);
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.sessionRefMin).toBe(100);
  });

  it("clearing a session ref bound emits null", () => {
    const filters = defaultSessionsFilters();
    filters.sessionRefMin = 50;
    const { onChange } = renderDropdown({ filters });

    fireEvent.change(screen.getByLabelText("Session ref minimum"), {
      target: { value: "" },
    });
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.sessionRefMin).toBeNull();
  });

  it("toggling a task ref role emits onChange with the role added to an empty default", () => {
    const { onChange } = renderDropdown();
    fireEvent.click(screen.getByLabelText("Created"));
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect([...next.taskRefRoles].sort()).toEqual(["created"]);
  });

  it("date preset SegmentedControl drives onChange", () => {
    const { onChange } = renderDropdown();
    fireEvent.click(screen.getByRole("radio", { name: "7d" }));
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.datePreset).toBe("7d");
  });

  it("date preset selection hides an open custom range", () => {
    const filters = defaultSessionsFilters();
    filters.datePreset = "custom";
    filters.dateCustomFrom = "2026-04-01";
    const { container, onChange } = renderDropdown({ filters });

    fireEvent.click(screen.getByRole("radio", { name: "7d" }));

    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.datePreset).toBe("7d");
    expect(container.querySelectorAll("input[type='date']").length).toBe(0);
  });

  it("syncs custom-range disclosure when filters change externally", () => {
    const { container, rerenderFilters } = renderDropdown();
    expect(container.querySelectorAll("input[type='date']").length).toBe(0);

    const customFilters = defaultSessionsFilters();
    customFilters.datePreset = "custom";
    rerenderFilters(customFilters);
    expect(container.querySelectorAll("input[type='date']").length).toBe(2);

    rerenderFilters(defaultSessionsFilters());
    expect(container.querySelectorAll("input[type='date']").length).toBe(0);
  });

  it("custom-range disclosure expands and reveals two date inputs", () => {
    const { container } = renderDropdown();
    fireEvent.click(screen.getByText(/Custom range/));
    // Two date inputs revealed
    const dateInputs = container.querySelectorAll("input[type='date']");
    expect(dateInputs.length).toBe(2);
  });

  it("custom-range disclosure selects custom when saved custom dates exist", () => {
    const filters = defaultSessionsFilters();
    filters.dateCustomFrom = "2026-04-01";
    const { onChange } = renderDropdown({ filters });

    fireEvent.click(screen.getByText(/Custom range/));

    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.datePreset).toBe("custom");
  });

  it("custom-range disclosure clears the preset when collapsed", () => {
    const filters = defaultSessionsFilters();
    filters.datePreset = "custom";
    filters.dateCustomFrom = "2026-04-01";
    const { onChange } = renderDropdown({ filters });

    fireEvent.click(screen.getByText(/Custom range/));

    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.datePreset).toBe("all");
  });

  it("Reset is disabled when no filters are active", () => {
    renderDropdown();
    const reset = screen.getByRole("button", { name: "Reset" });
    expect(reset).toBeDisabled();
  });

  it("Reset clears filters when active", () => {
    const filters = defaultSessionsFilters();
    filters.modes.add("auto");
    filters.providers.add("claude");
    const { onChange } = renderDropdown({ filters });

    const reset = screen.getByRole("button", { name: "Reset" });
    expect(reset).not.toBeDisabled();
    fireEvent.click(reset);
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.modes.size).toBe(0);
    expect(next.providers.size).toBe(0);
  });

  it("Apply calls onClose without mutating filters", () => {
    const { onChange, onClose } = renderDropdown();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("Escape closes the dropdown", () => {
    const { onClose } = renderDropdown();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("moves and traps focus in the mobile modal, then restores focus", async () => {
    vi.mocked(useIsMobile).mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([
      {} as DOMRect,
    ] as unknown as DOMRectList);
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();

    const { onClose, unmount } = renderDropdown();
    const first = screen.getByLabelText("Interactive");
    const last = screen.getByRole("button", { name: "Apply" });

    await waitFor(() => expect(first).toHaveFocus());
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(first).toHaveFocus();

    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();

    fireEvent.keyDown(last, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it("clicking the outside overlay closes the dropdown", () => {
    const { onClose } = renderDropdown();
    const overlay = screen.getByTestId("sessions-filter-overlay");
    fireEvent.click(overlay);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders empty hint when no providers are available", () => {
    renderDropdown({ providerOptions: [] });
    expect(screen.getByText("No providers available")).toBeInTheDocument();
  });

  it("custom date inputs surface filter values when present", () => {
    const filters = defaultSessionsFilters();
    filters.datePreset = "custom";
    filters.dateCustomFrom = "2026-04-01";
    filters.dateCustomTo = "2026-04-15";
    const { container } = renderDropdown({ filters });
    const inputs = container.querySelectorAll("input[type='date']");
    expect((inputs[0] as HTMLInputElement).value).toBe("2026-04-01");
    expect((inputs[1] as HTMLInputElement).value).toBe("2026-04-15");
  });

  it("Mode section uses unique labels per option", () => {
    renderDropdown();
    // Scoped queries for the Mode section avoid colliding with other
    // sections that have similar text.
    const section = screen.getByText("Mode").parentElement!;
    expect(within(section).getByText("Interactive")).toBeInTheDocument();
    expect(within(section).getByText("Autonomous")).toBeInTheDocument();
  });
});
