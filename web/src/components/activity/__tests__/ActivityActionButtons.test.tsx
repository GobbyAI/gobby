import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../ActivityActionsContext";
import {
  useRegisterActivityActions,
  type ActivityPanelActions,
} from "../activityActions";

function RegisteringTab({ actions }: { actions: ActivityPanelActions }) {
  useRegisterActivityActions(actions, [actions]);
  return null;
}

function renderHeader(actions: ActivityPanelActions) {
  return render(
    <ActivityActionsProvider>
      <ActivityActionButtons />
      <RegisteringTab actions={actions} />
    </ActivityActionsProvider>,
  );
}

describe("ActivityActionButtons", () => {
  it("renders nothing when the active tab registers no actions", () => {
    const { container } = render(
      <ActivityActionsProvider>
        <ActivityActionButtons />
      </ActivityActionsProvider>,
    );
    expect(container.querySelector(".activity-panel-actions-slot")).toBeNull();
  });

  it("renders the registered selector as the canonical SegmentedControl", () => {
    const onChange = vi.fn();
    renderHeader({
      selector: {
        value: "live",
        onChange,
        options: [
          { value: "live", label: "Live" },
          { value: "expired", label: "Expired" },
        ],
        ariaLabel: "Session status filter",
      },
    });

    const group = screen.getByRole("radiogroup", { name: "Session status filter" });
    expect(group).toHaveClass("segmented-control");
    expect(screen.getByRole("radio", { name: "Live" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    fireEvent.click(screen.getByRole("radio", { name: "Expired" }));
    expect(onChange).toHaveBeenCalledWith("expired");
  });

  it("renders Filter as a trigger with expanded state and applied-count badge", () => {
    const onToggle = vi.fn();
    renderHeader({
      filter: {
        open: true,
        onToggle,
        ariaLabel: "Filter sessions",
        activeCount: 2,
      },
    });

    const button = screen.getByRole("button", { name: "Filter sessions" });
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button.querySelector(".activity-filter-badge")?.textContent).toBe("2");
    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("omits the badge at zero applied filters", () => {
    renderHeader({
      filter: { open: false, onToggle: vi.fn(), ariaLabel: "Filter sessions" },
    });
    const button = screen.getByRole("button", { name: "Filter sessions" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button.querySelector(".activity-filter-badge")).toBeNull();
  });

  it("renders Search as a toggle with expanded state", () => {
    const onToggle = vi.fn();
    renderHeader({
      search: { open: false, onToggle, ariaLabel: "Search sessions" },
    });
    const button = screen.getByRole("button", { name: "Search sessions" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("labels the add action New by default", () => {
    renderHeader({ onAdd: vi.fn(), addAriaLabel: "New channel" });
    expect(
      screen.getByRole("button", { name: "New channel" }).textContent,
    ).toContain("New");
  });

  it("keeps the selector keyboard-operable with arrow keys", () => {
    function Tab() {
      const [value, setValue] = useState("live");
      useRegisterActivityActions(
        {
          selector: {
            value,
            onChange: setValue,
            options: [
              { value: "live", label: "Live" },
              { value: "expired", label: "Expired" },
            ],
            ariaLabel: "Session status filter",
          },
        },
        [value],
      );
      return null;
    }
    render(
      <ActivityActionsProvider>
        <ActivityActionButtons />
        <Tab />
      </ActivityActionsProvider>,
    );

    const live = screen.getByRole("radio", { name: "Live" });
    live.focus();
    fireEvent.keyDown(live, { key: "ArrowRight" });
    expect(screen.getByRole("radio", { name: "Expired" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});
