import { defineConfig } from "vite";
import tailwind from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";
export default defineConfig({
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  publicDir: "dist/public",
  plugins: [tailwind()],
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: {
      "@gobby/annotate-core": fileURLToPath(
        new URL("../core/src/index.ts", import.meta.url),
      ),
    },
  },
  build: {
    outDir: "dist/chrome",
    emptyOutDir: true,
    lib: {
      entry: "src/content.tsx",
      formats: ["iife"],
      name: "GobbyAnnotate",
      fileName: () => "content.js",
    },
    assetsInlineLimit: 2000000,
  },
});
