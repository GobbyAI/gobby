import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TasksMobileStageChips } from "../TasksMobileStageChips";
import type { StagePivotChip } from "../TasksTabModel";

const stages: StagePivotChip[] = [
  { name: "development", label: "Development", count: 3 },
  { name: "operator_review", label: "Operator Review", count: 1 },
];

function renderChips(
  activeStage: string | null | undefined,
  onSelect = vi.fn(),
) {
  render(
    <TasksMobileStageChips
      stages={stages}
      totalCount={4}
      activeStage={activeStage}
      onSelect={onSelect}
    />,
  );
  return onSelect;
}

describe("TasksMobileStageChips", () => {
  it("renders an All chip plus one chip per stage with counts", () => {
    renderChips(null);
    expect(screen.getByRole("button", { name: /All/ })).toHaveTextContent("4");
    expect(
      screen.getByRole("button", { name: /Development/ }),
    ).toHaveTextContent("3");
    expect(
      screen.getByRole("button", { name: /Operator Review/ }),
    ).toHaveTextContent("1");
  });

  it("marks the All chip pressed when activeStage is null", () => {
    renderChips(null);
    expect(screen.getByRole("button", { name: /All/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.getByRole("button", { name: /Development/ }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("marks the matching stage chip pressed", () => {
    renderChips("development");
    expect(
      screen.getByRole("button", { name: /Development/ }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /All/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("presses no chip for a custom multi-stage filter (undefined)", () => {
    renderChips(undefined);
    expect(screen.getByRole("button", { name: /All/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(
      screen.getByRole("button", { name: /Development/ }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("calls onSelect(null) for All and onSelect(name) for a stage", () => {
    const onSelect = renderChips("development");

    fireEvent.click(screen.getByRole("button", { name: /All/ }));
    expect(onSelect).toHaveBeenCalledWith(null);

    fireEvent.click(screen.getByRole("button", { name: /Operator Review/ }));
    expect(onSelect).toHaveBeenCalledWith("operator_review");
  });
});
