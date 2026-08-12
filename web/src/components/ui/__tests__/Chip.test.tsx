import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Chip } from "../Chip";
import { chipIdentityClasses, chipVariants } from "../chipVariants";

describe("Chip", () => {
  it("renders a span with the shared pill geometry", () => {
    render(<Chip>open</Chip>);

    const chip = screen.getByText("open");
    expect(chip.tagName).toBe("SPAN");
    expect(chip).toHaveClass(
      "inline-flex",
      "h-5",
      "items-center",
      "justify-center",
      "rounded-full",
      "px-1.5",
      "font-semibold",
      "leading-none",
      "whitespace-nowrap",
    );
  });

  it("defaults to the neutral tone with lowercase presentation", () => {
    render(<Chip>chore</Chip>);

    const chip = screen.getByText("chore");
    expect(chip.className).toContain("var(--text-muted)");
    expect(chip).not.toHaveClass("uppercase");
  });

  it.each([
    ["accent", "var(--accent)"],
    ["info", "var(--color-info)"],
    ["warning", "var(--color-warning-foreground)"],
    ["error", "var(--color-error)"],
  ] as const)("colors the %s tone from its state token", (tone, token) => {
    render(<Chip tone={tone}>label</Chip>);

    const chip = screen.getByText("label");
    expect(chip.className).toContain(`text-[color:${token}]`);
    expect(chip.className).toContain(
      `bg-[color-mix(in_srgb,${token}_15%,transparent)]`,
    );
  });

  it("applies uppercase presentation only when requested", () => {
    render(<Chip uppercase>tmux</Chip>);

    expect(screen.getByText("tmux")).toHaveClass("uppercase");
  });

  it("renders through the child element with asChild", () => {
    render(
      <Chip asChild tone="info">
        <button type="button">filter</button>
      </Chip>,
    );

    const chip = screen.getByRole("button", { name: "filter" });
    expect(chip).toHaveClass("rounded-full", "h-5");
    expect(chip.className).toContain("var(--color-info)");
  });

  it("lets call-site classes win over the tone fill", () => {
    render(
      <Chip tone="warning" className="bg-[var(--color-warning-soft)]">
        blocked 2
      </Chip>,
    );

    const chip = screen.getByText("blocked 2");
    expect(chip.className).toContain("bg-[var(--color-warning-soft)]");
    expect(chip.className).not.toContain(
      "bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)]",
    );
    expect(chip.className).toContain(
      "text-[color:var(--color-warning-foreground)]",
    );
  });

  it("composes the session identity treatment with the accent tone", () => {
    render(
      <Chip tone="accent" uppercase className={chipIdentityClasses}>
        web
      </Chip>,
    );

    const chip = screen.getByText("web");
    expect(chip).toHaveClass(
      "font-mono",
      "border",
      "border-border",
      "uppercase",
    );
    // Dark keeps the accent "on" fill; the light flip rides its own variant
    // scope so twMerge must not swallow either side.
    expect(chip.className).toContain("text-[color:var(--accent)]");
    expect(chip.className).toContain("[[data-theme=light]_&]:bg-muted");
    expect(chip.className).toContain(
      "[[data-theme=light]_&]:text-muted-foreground",
    );
  });
});

// 3.1.4 — state-bearing tones carry a non-hue cue. The tone ladder's text
// tokens are asserted against .impeccable.md's locked palette: the
// deutan-confusable amber/green pair (warning vs accent) and warning vs
// destructive keep a lightness step in BOTH themes, and the dark ladder is
// pairwise lightness-separated, so tones never rely on hue alone.
describe("Chip tone ladder non-hue cues", () => {
  const tokensCss = readFileSync(
    resolve(process.cwd(), "src/styles/tokens.css"),
    "utf8",
  );
  const lightStart = tokensCss.indexOf('[data-theme="light"]');
  const themes = {
    dark: tokensCss.slice(0, lightStart),
    light: tokensCss.slice(lightStart),
  };

  const STATE_TONE_TOKENS = {
    accent: "accent",
    info: "color-info",
    warning: "color-warning-foreground",
    error: "color-error",
  } as const;

  function lightness(themeCss: string, name: string): number {
    const match = new RegExp(`--${name}:\\s*oklch\\((\\d+(?:\\.\\d+)?)%`).exec(
      themeCss,
    );
    if (!match) throw new Error(`Missing oklch lightness for --${name}`);
    return Number(match[1]);
  }

  it.each(Object.entries(STATE_TONE_TOKENS))(
    "wires the %s tone to its palette token",
    (tone, token) => {
      expect(
        chipVariants({ tone: tone as keyof typeof STATE_TONE_TOKENS }),
      ).toContain(`var(--${token})`);
    },
  );

  it.each(["dark", "light"] as const)(
    "keeps a lightness step between the deutan-confusable pairs in %s",
    (theme) => {
      const themeCss = themes[theme];
      const accent = lightness(themeCss, STATE_TONE_TOKENS.accent);
      const warning = lightness(themeCss, STATE_TONE_TOKENS.warning);
      const error = lightness(themeCss, STATE_TONE_TOKENS.error);

      // Amber vs green is the deutan collision axis; magenta was chosen to
      // survive it, but still keeps distinct lightness from warning.
      expect(Math.abs(accent - warning)).toBeGreaterThanOrEqual(4);
      expect(Math.abs(warning - error)).toBeGreaterThanOrEqual(3);
    },
  );

  it("separates every dark-theme tone pair by lightness", () => {
    const values = Object.values(STATE_TONE_TOKENS).map((token) =>
      lightness(themes.dark, token),
    );
    for (let i = 0; i < values.length; i += 1) {
      for (let j = i + 1; j < values.length; j += 1) {
        expect(Math.abs(values[i] - values[j])).toBeGreaterThanOrEqual(4);
      }
    }
  });
});
