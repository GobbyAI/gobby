import path from "node:path";
import stylelint from "stylelint";
import { describe, expect, it } from "vitest";

const ruleName = "gobby/require-reduced-motion-reset";
const pluginPath = path.resolve(process.cwd(), "stylelint-gobby-rules.cjs");

async function lintCss(codeFilename: string) {
  return stylelint.lint({
    code: ".animated { animation-duration: 1s; }",
    codeFilename,
    config: {
      plugins: [pluginPath],
      rules: {
        [ruleName]: true,
      },
    },
  });
}

describe("stylelint gobby rules", () => {
  it("reports missing reduced-motion reset only for the exact base stylesheet", async () => {
    const exactBaseCssPath = path.resolve(process.cwd(), "src/styles/base.css");
    const nestedBaseCssPath = path.resolve(
      process.cwd(),
      "tmp/src/styles/base.css",
    );

    const exactResult = await lintCss(exactBaseCssPath);
    const nestedResult = await lintCss(nestedBaseCssPath);

    expect(exactResult.results[0].warnings).toHaveLength(1);
    expect(nestedResult.results[0].warnings).toHaveLength(0);
  });
});
