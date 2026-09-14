import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  contrastRatio,
  contrastRatioOnSrgbTint,
} from "../../lib/colorContrast";

const AA_NORMAL_TEXT = 4.5;
const AA_NON_TEXT = 3;
const tokensCss = readFileSync(
  resolve(process.cwd(), "src/styles/tokens.css"),
  "utf8",
);
const lightThemeStart = tokensCss.indexOf('[data-theme="light"]');
const themes = {
  dark: tokensCss.slice(0, lightThemeStart),
  light: tokensCss.slice(lightThemeStart),
} as const;

function themeToken(theme: keyof typeof themes, name: string): string {
  const match = new RegExp(`--${name}:\\s*(oklch\\([^;]+\\));`).exec(
    themes[theme],
  );
  if (!match) throw new Error(`Missing ${theme}-theme token --${name}`);
  return match[1];
}

function token(name: string): string {
  return themeToken("light", name);
}

const surfaces = ["bg-primary", "bg-secondary", "bg-tertiary"] as const;

describe("light-theme semantic token contrast", () => {
  it.each(surfaces)(
    "keeps warning text AA on %s and its warning tint",
    (surfaceName) => {
      const foreground = token("color-warning-foreground");
      const surface = token(surfaceName);

      expect(contrastRatio(foreground, surface)).toBeGreaterThanOrEqual(
        AA_NORMAL_TEXT,
      );
      expect(
        contrastRatioOnSrgbTint(foreground, foreground, 0.1, surface),
      ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    },
  );

  it("uses dark ink on the light warning surface", () => {
    expect(
      contrastRatio(token("text-on-warning"), token("color-warning-bg")),
    ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
  });

  it.each([
    ["accent", 0.14],
    ["color-error", 0.14],
  ] as const)(
    "keeps %s badge text AA on tertiary and tinted surfaces",
    (tokenName, alpha) => {
      const foreground = token(tokenName);
      const tertiary = token("bg-tertiary");

      expect(contrastRatio(foreground, tertiary)).toBeGreaterThanOrEqual(
        AA_NORMAL_TEXT,
      );
      expect(
        contrastRatioOnSrgbTint(foreground, foreground, alpha, tertiary),
      ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    },
  );
});

describe.each(["dark", "light"] as const)(
  "%s-theme terminal typing controls",
  (theme) => {
    it("keeps quick-key labels AA on the keycap", () => {
      expect(
        contrastRatio(
          themeToken(theme, "quick-key-foreground"),
          themeToken(theme, "quick-key"),
        ),
      ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    });

    it("keeps the Expand label AA on its accent tint in the status bar", () => {
      const accent = themeToken(theme, "accent");
      expect(
        contrastRatioOnSrgbTint(
          accent,
          accent,
          0.1,
          themeToken(theme, "bg-secondary"),
        ),
      ).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    });

    it("keeps the solid keyboard key, its ring, and its glyph at 3:1", () => {
      const accent = themeToken(theme, "accent");
      expect(
        contrastRatio(accent, themeToken(theme, "bg-primary")),
      ).toBeGreaterThanOrEqual(AA_NON_TEXT);
      expect(
        contrastRatio(themeToken(theme, "accent-foreground"), accent),
      ).toBeGreaterThanOrEqual(AA_NON_TEXT);
    });
  },
);
