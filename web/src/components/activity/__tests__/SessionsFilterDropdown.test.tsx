import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

import { SessionsFilterDropdown } from "../SessionsFilterDropdown";
import { defaultSessionsFilters, type SessionsFilters } from "../sessionsFilters";

function renderDropdown(overrides: Partial<{
  filters: SessionsFilters;
  providerOptions: readonly string[];
  statusMode: "live" | "expired";
}> = {}) {
  const onChange = vi.fn();
  const onClose = vi.fn();
  const onStatusModeChange = vi.fn();
  const view = render(
    <SessionsFilterDropdown
      filters={overrides.filters ?? defaultSessionsFilters()}
      onChange={onChange}
      providerOptions={overrides.providerOptions ?? ["claude", "codex", "gemini"]}
      statusMode={overrides.statusMode ?? "live"}
      onStatusModeChange={onStatusModeChange}
      onClose={onClose}
    />,
  );
  return { onChange, onClose, onStatusModeChange, ...view };
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

  it("renders providers alphabetically and checked by default", () => {
    renderDropdown({ providerOptions: ["gemini", "claude", "codex"] });

    const section = screen.getByText("Provider").parentElement!;
    expect(
      within(section)
        .getAllByRole("checkbox")
        .map((checkbox) => checkbox.nextSibling?.textContent),
    ).toEqual(["claude", "codex", "gemini"]);
    expect(screen.getByLabelText("claude")).toBeChecked();
    expect(screen.getByLabelText("codex")).toBeChecked();
    expect(screen.getByLabelText("gemini")).toBeChecked();
  });

  it("toggling a default Provider emits the remaining narrowed provider set", () => {
    const { onChange } = renderDropdown();
    fireEvent.click(screen.getByLabelText("codex"));

    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect([...next.providers]).toEqual(["claude", "gemini"]);
  });

  it("toggling a default Provider uses the sorted provider option order", () => {
    const { onChange } = renderDropdown({ providerOptions: ["gemini", "claude", "codex"] });
    fireEvent.click(screen.getByLabelText("codex"));

    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect([...next.providers]).toEqual(["claude", "gemini"]);
  });

  it("typing a session ref bound emits onChange with the parsed integer", () => {
    const { onChange } = renderDropdown();
    fireEvent.change(screen.getByLabelText("Session ref minimum", { selector: "input" })!, {
      target: { value: "100" },
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.sessionRefMin).toBe(100);
  });

  it("clearing a session ref bound emits null", () => {
    const filters = defaultSessionsFilters();
    filters.sessionRefMin = 50;
    const { onChange } = renderDropdown({ filters });

    fireEvent.change(screen.getByLabelText("Session ref minimum"), { target: { value: "" } });
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.sessionRefMin).toBeNull();
  });

  it("toggling a task ref role emits onChange with updated roles", () => {
    const { onChange } = renderDropdown();
    fireEvent.click(screen.getByLabelText("Created"));
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect([...next.taskRefRoles].sort()).toEqual(["claimed", "created"]);
  });

  it("date preset SegmentedControl drives onChange", () => {
    const { onChange } = renderDropdown();
    fireEvent.click(screen.getByRole("radio", { name: "7d" }));
    const next: SessionsFilters = onChange.mock.calls[0][0];
    expect(next.datePreset).toBe("7d");
  });

  it("custom-range disclosure expands and reveals two date inputs", () => {
    const { container } = renderDropdown();
    fireEvent.click(screen.getByText(/Custom range/));
    // Two date inputs revealed
    const dateInputs = container.querySelectorAll("input[type='date']");
    expect(dateInputs.length).toBe(2);
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
