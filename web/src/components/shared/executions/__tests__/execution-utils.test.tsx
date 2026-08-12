import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  StatusBadge,
  StepDisplay,
  StepStatusIcon,
  type StepData,
} from "../execution-utils";

const STEP: StepData = {
  id: 1,
  step_id: "build",
  status: "completed",
  started_at: null,
  completed_at: null,
  output_json: null,
  error: null,
};

describe("StepStatusIcon", () => {
  it.each([
    ["completed", "polyline"],
    ["failed", "line"],
    ["running", ".animate-spin"],
    ["waiting_approval", "polyline"],
    ["skipped", "polygon"],
    ["timeout", "circle"],
    ["pending", "circle"],
  ])("renders a labeled glyph for %s", (status, glyphSelector) => {
    const { container } = render(<StepStatusIcon status={status} />);

    expect(
      screen.getByRole("img", {
        name: `Step status: ${status.replace(/_/g, " ")}`,
      }),
    ).toBeInTheDocument();
    expect(container.querySelector(glyphSelector)).toBeInTheDocument();
  });

  it("uses structurally distinct glyphs when color is unavailable", () => {
    const statuses = [
      "completed",
      "failed",
      "running",
      "waiting_approval",
      "skipped",
      "timeout",
      "pending",
    ];
    const glyphs = statuses.map((status) => {
      const { container, unmount } = render(<StepStatusIcon status={status} />);
      const markup = container.querySelector("svg")?.outerHTML;
      unmount();
      return markup;
    });

    expect(new Set(glyphs).size).toBe(statuses.length);
  });
});

describe("StepDisplay", () => {
  it.each(["card", "timeline"] as const)(
    "renders status semantics in the %s layout",
    (layout) => {
      render(<StepDisplay step={STEP} index={0} layout={layout} />);

      expect(
        screen.getByRole("img", { name: "Step status: completed" }),
      ).toBeInTheDocument();
    },
  );

  it("renders one status glyph in the timeline layout", () => {
    render(<StepDisplay step={STEP} index={0} layout="timeline" />);

    expect(
      screen.getAllByRole("img", { name: "Step status: completed" }),
    ).toHaveLength(1);
  });

  it("uses a native button for the expandable step header", () => {
    render(<StepDisplay step={STEP} index={0} />);

    expect(screen.getByRole("button", { name: /build/ }).tagName).toBe(
      "BUTTON",
    );
  });
});

describe("StatusBadge", () => {
  it("uses the shared status-chip geometry", () => {
    render(<StatusBadge status="completed" />);

    expect(screen.getByText("Completed")).toHaveClass(
      "inline-flex",
      "items-center",
      "rounded-full",
    );
  });
});
