import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  FilterDropdownTrigger,
  InlineFilterFieldRow,
  InlineFilterPanel,
} from "../FilterPrimitives";

describe("FilterPrimitives", () => {
  it("renders the shared inline panel shell and field row", () => {
    render(
      <InlineFilterPanel aria-label="Example filters">
        <InlineFilterFieldRow>
          <label htmlFor="example-filter">Status</label>
          <select id="example-filter">
            <option>All</option>
          </select>
        </InlineFilterFieldRow>
      </InlineFilterPanel>,
    );

    const panel = screen.getByLabelText("Example filters");
    expect(panel).toHaveClass("absolute", "grid", "border-border", "shadow-md");
    expect(screen.getByText("Status").parentElement).toHaveClass(
      "grid",
      "min-w-0",
      "text-[length:var(--text-xs)]",
    );
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("keeps the activity filter badge styling hook on the trigger", () => {
    render(<FilterDropdownTrigger open={false} activeCount={2} />);

    expect(screen.getByText("2")).toHaveClass("activity-filter-badge");
  });
});
