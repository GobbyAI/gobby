import { describe, expect, it } from "vitest";

import { contrastRatio, parseOklch, relativeLuminance } from "../colorContrast";

/**
 * These anchors guard the hand-rolled OKLab → sRGB matrices against typos:
 * a sign error in any coefficient would move the known black/white luminance
 * and the canonical 21:1 black-on-white ratio off their fixed values.
 */
describe("colorContrast", () => {
  it("parses oklch() strings into normalized components", () => {
    expect(parseOklch("oklch(62% 0.005 125)")).toEqual({
      l: 0.62,
      c: 0.005,
      h: 125,
    });
    expect(parseOklch("  oklch(100% 0 0)  ")).toEqual({ l: 1, c: 0, h: 0 });
  });

  it("throws on non-oklch input", () => {
    expect(() => parseOklch("#abcdef")).toThrow();
    expect(() => parseOklch("rgb(0 0 0)")).toThrow();
  });

  it("anchors relative luminance at black and white", () => {
    expect(relativeLuminance("oklch(0% 0 0)")).toBeCloseTo(0, 5);
    expect(relativeLuminance("oklch(100% 0 0)")).toBeCloseTo(1, 5);
  });

  it("places a mid gray between black and white", () => {
    const mid = relativeLuminance("oklch(50% 0 0)");
    expect(mid).toBeGreaterThan(0.1);
    expect(mid).toBeLessThan(0.25);
  });

  it("returns the canonical 21:1 ratio for black on white", () => {
    expect(contrastRatio("oklch(0% 0 0)", "oklch(100% 0 0)")).toBeCloseTo(
      21,
      1,
    );
  });

  it("is order-independent", () => {
    const a = "oklch(62% 0.005 125)";
    const b = "oklch(11% 0.005 125)";
    expect(contrastRatio(a, b)).toBe(contrastRatio(b, a));
  });

  it("matches the expected ratio for the gutter token on the code background", () => {
    // Independently verifiable in a WCAG/canvas-blended checker.
    expect(
      contrastRatio("oklch(62% 0.005 125)", "oklch(11% 0.005 125)"),
    ).toBeCloseTo(5.63, 1);
  });
});
