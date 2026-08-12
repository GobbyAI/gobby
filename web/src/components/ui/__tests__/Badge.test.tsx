import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Badge } from "../Badge";

describe("Badge", () => {
  it("uses semantic info colors for the info variant", () => {
    render(<Badge variant="info">Info</Badge>);

    expect(screen.getByText("Info")).toHaveClass(
      "bg-[var(--color-info-soft)]",
      "text-[var(--color-info)]",
    );
  });
});
