import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
export default defineConfig({
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: {
      "@gobby/annotate-core": fileURLToPath(
        new URL("./packages/core/src/index.ts", import.meta.url),
      ),
      react: fileURLToPath(new URL("./node_modules/react", import.meta.url)),
      "react-dom": fileURLToPath(
        new URL("./node_modules/react-dom", import.meta.url),
      ),
    },
  },
  test: { include: ["packages/*/test/**/*.test.{ts,tsx}"], restoreMocks: true },
});
