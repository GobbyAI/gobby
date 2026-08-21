import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { contrastRatio } from "../lib/colorContrast";

/**
 * `.capability-chip` paints an explicit opaque pair rather than the neutral
 * Chip tint, so its text contrast is independent of the row underneath (the
 * picker hovers rows with bg-muted). Pin the pair and its AA ratio per theme.
 */
const tokensCss = readFileSync(
  join(process.cwd(), "src/styles/tokens.css"),
  "utf8",
);
const baseCss = readFileSync(
  join(process.cwd(), "src/styles/base.css"),
  "utf8",
);

function themeBlock(selector: RegExp): string {
  const match = tokensCss.match(selector);
  if (!match) throw new Error(`Unable to find theme block ${selector}`);
  return match[1] ?? "";
}

function token(block: string, name: string): string {
  const match = block.match(
    new RegExp(`${name.replace(/[-]/g, "\\-")}:\\s*(oklch\\([^)]*\\))`),
  );
  if (!match?.[1]) throw new Error(`Unable to find token ${name}`);
  return match[1];
}

function chipRule(): string {
  const match = baseCss.match(/\.capability-chip\s*{([^}]*)}/);
  if (!match?.[1]) throw new Error("Unable to find .capability-chip rule");
  return match[1];
}

describe("capability chip contrast", () => {
  const rule = chipRule();

  it("uses an explicit opaque token pair with a border", () => {
    expect(rule).toMatch(/background:\s*var\(--bg-primary\)/);
    expect(rule).toMatch(/color:\s*var\(--text-secondary\)/);
    expect(rule).toMatch(/border:\s*1px solid var\(--border\)/);
  });

  it.each([
    ["dark", /^:root\s*{([\s\S]*?)^}/m],
    ["light", /\[data-theme="light"\]\s*{([\s\S]*?)^}/m],
  ])("holds WCAG AA (4.5:1) in the %s theme", (_theme, selector) => {
    const block = themeBlock(selector);
    const ratio = contrastRatio(
      token(block, "--text-secondary"),
      token(block, "--bg-primary"),
    );
    expect(ratio).toBeGreaterThanOrEqual(4.5);
  });
});
