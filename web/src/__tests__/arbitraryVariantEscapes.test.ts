import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Tailwind converts bare `_` to a space inside arbitrary variants, so a
// selector like `[&_.activity-list-row__body]:flex` silently compiles to the
// dead selector `& .activity-list-row  body`. Literal underscores in targeted
// class names must be escaped (`\_`), and the containing literal must be
// String.raw (or a JSX attribute) so the backslash survives to the DOM.
// This scan fails on any class-token that mixes `&`, a bare `__`, and a
// variant terminator `]:` — the signature of an unescaped BEM target.

const SRC_ROOT = join(process.cwd(), "src");

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "__tests__" || entry.name === "node_modules") continue;
      out.push(...sourceFiles(path));
    } else if (
      /\.(ts|tsx)$/.test(entry.name) &&
      !/\.test\.(ts|tsx)$/.test(entry.name)
    ) {
      out.push(path);
    }
  }
  return out;
}

describe("arbitrary variant underscore escapes", () => {
  it("has no unescaped __ class targets inside &-variants in production sources", () => {
    const violations: string[] = [];
    for (const file of sourceFiles(SRC_ROOT)) {
      const lines = readFileSync(file, "utf8").split("\n");
      lines.forEach((line, i) => {
        for (const token of line.split(/[\s'"`]+/)) {
          if (
            token.includes("&") &&
            token.includes("__") &&
            token.includes("]:")
          ) {
            violations.push(
              `${file.slice(SRC_ROOT.length + 1)}:${i + 1} ${token}`,
            );
          }
        }
      });
    }
    expect(violations).toEqual([]);
  });
});
