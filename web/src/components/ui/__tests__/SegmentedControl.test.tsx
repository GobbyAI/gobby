import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SegmentedControl } from "../SegmentedControl";
import { coarseHitAreaCls } from "../controlStyles";

const HIT_AREA_TOKENS = coarseHitAreaCls.split(/\s+/).filter(Boolean);

const OPTIONS = [
  { value: "a", label: "A" },
  { value: "b", label: "B" },
  { value: "c", label: "C" },
] as const;

type OptionValue = (typeof OPTIONS)[number]["value"];

function renderControl(
  override: Partial<Parameters<typeof SegmentedControl<OptionValue>>[0]> = {},
) {
  const onChange = vi.fn();
  const utils = render(
    <SegmentedControl<OptionValue>
      value="a"
      onChange={onChange}
      options={OPTIONS}
      ariaLabel="Letter"
      {...override}
    />,
  );
  return { onChange, ...utils };
}

describe("SegmentedControl", () => {
  it("renders all options with one aria-checked", () => {
    renderControl();
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(3);
    expect(radios.map((r) => r.getAttribute("aria-checked"))).toEqual([
      "true",
      "false",
      "false",
    ]);
  });

  it("fires onChange when an inactive option is clicked", () => {
    const { onChange } = renderControl();
    fireEvent.click(screen.getByRole("radio", { name: "B" }));
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("fires per-option onClick in addition to onChange", () => {
    const onChange = vi.fn();
    const sideEffect = vi.fn();
    render(
      <SegmentedControl<OptionValue>
        value="a"
        onChange={onChange}
        options={[
          { value: "a", label: "A" },
          { value: "b", label: "B", onClick: sideEffect },
        ]}
        ariaLabel="Letter"
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "B" }));
    expect(onChange).toHaveBeenCalledWith("b");
    expect(sideEffect).toHaveBeenCalledTimes(1);
  });

  it("ArrowRight wraps from last to first", () => {
    const { onChange } = renderControl({ value: "c" });
    const last = screen.getByRole("radio", { name: "C" });
    fireEvent.keyDown(last, { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("a");
  });

  it("ArrowLeft wraps from first to last", () => {
    const { onChange } = renderControl({ value: "a" });
    const first = screen.getByRole("radio", { name: "A" });
    fireEvent.keyDown(first, { key: "ArrowLeft" });
    expect(onChange).toHaveBeenCalledWith("c");
  });

  it("Home jumps to first, End jumps to last", () => {
    const { onChange } = renderControl({ value: "b" });
    const middle = screen.getByRole("radio", { name: "B" });
    fireEvent.keyDown(middle, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith("a");
    fireEvent.keyDown(middle, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("c");
  });

  it("disabled state suppresses click and key handling", () => {
    const { onChange } = renderControl({ disabled: true });
    fireEvent.click(screen.getByRole("radio", { name: "B" }));
    fireEvent.keyDown(screen.getByRole("radio", { name: "A" }), {
      key: "ArrowRight",
    });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("active option is the roving tab stop", () => {
    renderControl({ value: "b" });
    const active = screen.getByRole("radio", { name: "B" });
    expect(active).toHaveAttribute("aria-checked", "true");
    expect(active).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "A" })).toHaveAttribute(
      "tabindex",
      "-1",
    );
    expect(screen.getByRole("radio", { name: "C" })).toHaveAttribute(
      "tabindex",
      "-1",
    );
  });

  it("expands option hit areas on coarse pointers without inflating the box (#19181)", () => {
    renderControl();

    // The track and options keep their visual height on touch — no coarse
    // min-size promotion anywhere; the 44px floor lives on the invisible
    // coarseHitAreaCls ::before expansion carried by each option.
    expect(screen.getByRole("radiogroup", { name: "Letter" })).not.toHaveClass(
      "pointer-coarse:min-h-11",
    );
    for (const radio of screen.getAllByRole("radio")) {
      for (const token of HIT_AREA_TOKENS) {
        expect(radio).toHaveClass(token);
      }
      expect(radio).not.toHaveClass("pointer-coarse:min-h-11");
      expect(radio).not.toHaveClass("pointer-coarse:min-w-11");
    }
  });

  it("omits the coarse hit-area expansion when dense chrome opts out", () => {
    renderControl({ coarseTouchTarget: false });

    for (const radio of screen.getAllByRole("radio")) {
      expect(radio).not.toHaveClass("pointer-coarse:before:content-['']");
      expect(radio).not.toHaveClass("pointer-coarse:before:min-h-11");
      expect(radio).not.toHaveClass("pointer-coarse:before:min-w-11");
    }
  });

  it("uses ariaLabel for non-text labels", () => {
    render(
      <SegmentedControl<"grid">
        value="grid"
        onChange={vi.fn()}
        options={[
          {
            value: "grid",
            label: <span aria-hidden="true">#</span>,
            ariaLabel: "Grid view",
          },
        ]}
        ariaLabel="View mode"
      />,
    );

    expect(
      screen.getByRole("radio", { name: "Grid view" }),
    ).toBeInTheDocument();
  });

  it("falls back to text labels when ariaLabel is blank", () => {
    render(
      <SegmentedControl<"a">
        value="a"
        onChange={vi.fn()}
        options={[{ value: "a", label: "Readable label", ariaLabel: "   " }]}
        ariaLabel="Letters"
      />,
    );

    expect(
      screen.getByRole("radio", { name: "Readable label" }),
    ).toBeInTheDocument();
  });

  it("keeps squeezed options from painting over neighbors", () => {
    // A flex parent that squeezes the track must shrink the options
    // (min-w-0) and ellipsize their labels rather than let them overflow
    // onto adjacent header controls (#20044).
    renderControl();
    for (const radio of screen.getAllByRole("radio")) {
      expect(radio).toHaveClass("min-w-0");
      const label = radio.querySelector(".segmented-control__option-label");
      expect(label).not.toBeNull();
      expect(label).toHaveClass("min-w-0", "truncate");
    }
  });
});
