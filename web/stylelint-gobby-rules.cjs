const stylelint = require("stylelint");
const path = require("node:path");

const ruleName = "gobby/require-reduced-motion-reset";
const baseCssPath = path.resolve(__dirname, "src/styles/base.css");

const messages = stylelint.utils.ruleMessages(ruleName, {
  missing:
    "src/styles/base.css must include a reduced-motion media query that limits animation and transition duration.",
});

const rule = stylelint.createPlugin(ruleName, (enabled) => {
  return (root, result) => {
    const validOptions = stylelint.utils.validateOptions(result, ruleName, {
      actual: enabled,
      possible: [true, false],
    });
    if (!validOptions || !enabled) return;

    const filePath = root.source && root.source.input.file;
    if (!filePath || path.resolve(filePath) !== baseCssPath) return;

    let hasRequiredReset = false;
    root.walkAtRules("media", (atRule) => {
      if (
        !atRule.params.includes("prefers-reduced-motion") ||
        !atRule.params.includes("reduce")
      ) {
        return;
      }

      const declarations = new Set();
      atRule.walkDecls((decl) => {
        declarations.add(decl.prop);
      });
      hasRequiredReset =
        hasRequiredReset ||
        (declarations.has("animation-duration") &&
          declarations.has("animation-iteration-count") &&
          declarations.has("transition-duration"));
    });

    if (!hasRequiredReset) {
      stylelint.utils.report({
        message: messages.missing,
        node: root,
        result,
        ruleName,
      });
    }
  };
});

module.exports = [rule];
module.exports.ruleName = ruleName;
module.exports.messages = messages;
