/// <reference types="vitest/config" />
import { createLogger, defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

import { proxyAwareErrorLogger } from "./src/lib/devProxyLogger";

const GOBBY_HTTP_PORT = process.env.GOBBY_DAEMON_PORT || "60887";
const GOBBY_UI_HOST = process.env.GOBBY_UI_HOST || "localhost";
const logger = createLogger();
logger.error = proxyAwareErrorLogger(logger);

// https://vite.dev/config/
export default defineConfig({
  customLogger: logger,
  plugins: [react(), tailwindcss()],
  resolve: {
    dedupe: ["@codemirror/state", "@codemirror/view", "@codemirror/language"],
  },
  server: {
    host: GOBBY_UI_HOST,
    port: 60889,
    hmr: {
      path: "/__vite_hmr",
    },
    allowedHosts:
      GOBBY_UI_HOST === "0.0.0.0"
        ? true
        : [
            "localhost",
            ".ts.net",
            ...(process.env.VITE_ALLOWED_HOST
              ? [process.env.VITE_ALLOWED_HOST]
              : []),
          ],
    proxy: {
      // Proxy API requests to Gobby daemon
      ...Object.fromEntries(
        [
          "/api",
          "/mcp",
          "/admin",
          "/tasks",
          "/sessions",
          "/memories",
          "/skills",
        ].map((path) => [
          path,
          { target: `http://localhost:${GOBBY_HTTP_PORT}`, changeOrigin: true },
        ]),
      ),
      // Proxy WebSocket through the daemon's HTTP /ws route rather than the
      // raw WebSocket port: the raw server requires a Bearer Authorization
      // header (unavailable to browser WebSockets), while the HTTP route
      // authenticates the session cookie and injects the token itself.
      "/ws": {
        target: `ws://localhost:${GOBBY_HTTP_PORT}`,
        ws: true,
        rewriteWsOrigin: true,
      },
    },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary", "lcov"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/__tests__/**",
        "src/test/**",
        "src/vite-env.d.ts",
      ],
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Largest chunks are intentional lazy/vendor bundles; keep this just above
    // the current main bundle so future growth still warns.
    chunkSizeWarningLimit: 1320,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (
              id.includes("/react/") ||
              id.includes("/react-dom/") ||
              id.includes("/scheduler/")
            )
              return "vendor-react";
            if (id.includes("@codemirror") || id.includes("codemirror"))
              return "vendor-codemirror";
            if (id.includes("@wterm")) return "vendor-wterm";
            if (
              id.includes("react-syntax-highlighter") ||
              id.includes("refractor") ||
              id.includes("prismjs") ||
              id.includes("highlight.js")
            )
              return "vendor-syntax";
            if (
              id.includes("react-markdown") ||
              id.includes("remark") ||
              id.includes("rehype") ||
              id.includes("unified") ||
              id.includes("mdast") ||
              id.includes("hast") ||
              id.includes("micromark") ||
              id.includes("marked")
            )
              return "vendor-markdown";
            if (id.includes("d3-") || id.includes("@dagrejs"))
              return "vendor-d3";
            if (id.includes("react-virtuoso")) return "vendor-virtualization";
          }
        },
      },
    },
  },
});
