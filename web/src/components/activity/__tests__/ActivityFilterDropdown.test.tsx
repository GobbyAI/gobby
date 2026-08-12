import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ActivityFilterDropdown } from "../ActivityFilterDropdown";

describe("ActivityFilterDropdown", () => {
  it("applies a selected option immediately before closing", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onClose = vi.fn();

    render(
      <ActivityFilterDropdown
        value="all"
        options={[
          { value: "all", label: "All activity" },
          { value: "active", label: "Active only" },
        ]}
        onChange={onChange}
        onClose={onClose}
        ariaLabel="Activity filters"
      />,
    );

    expect(
      screen.getByRole("option", { name: "All activity" }),
    ).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getByRole("option", { name: "Active only" }));

    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith("active");
    expect(onClose).toHaveBeenCalledOnce();
    expect(onChange.mock.invocationCallOrder[0]).toBeLessThan(
      onClose.mock.invocationCallOrder[0] ?? Number.POSITIVE_INFINITY,
    );
  });
});
