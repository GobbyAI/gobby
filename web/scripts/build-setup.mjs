/**
 * Build the Ink setup wizard into a single-file ESM bundle.
 *
 * Outputs:
 *   - web/dist-setup/cli.mjs          (for npm publish as @gobby/setup)
 *   - src/gobby/install/shared/setup/setup.mjs  (bundled in Python package)
 */
import { build } from "esbuild";
import { cpSync, mkdirSync, writeFileSync, readFileSync } from "fs";
import { dirname, join, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const projectRoot = resolve(root, "..");

// Read version from pyproject.toml
const pyproject = readFileSync(join(projectRoot, "pyproject.toml"), "utf-8");
const versionMatch = pyproject.match(/^version\s*=\s*"([^"]+)"/m);
const version = versionMatch ? versionMatch[1] : "0.0.0";

// Output directories
const distSetup = join(root, "dist-setup");
const pyBundle = join(projectRoot, "src", "gobby", "install", "shared", "setup");
const distBundlePath = join(distSetup, "cli.mjs");
const pyBundlePath = join(pyBundle, "setup.mjs");

mkdirSync(distSetup, { recursive: true });
mkdirSync(pyBundle, { recursive: true });

console.log(`Building setup wizard v${version}...`);

await build({
  entryPoints: [join(root, "src", "setup", "cli.tsx")],
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node18",
  outfile: distBundlePath,
  banner: {
    js: "#!/usr/bin/env node",
  },
  define: {
    "process.env.GOBBY_VERSION": JSON.stringify(version),
  },
  // Externalize optional peer deps and native modules that can't be bundled
  external: ["react-devtools-core"],
  // Handle WASM files (used by yoga-wasm-web for Ink layout)
  loader: {
    ".wasm": "binary",
  },
  // Minify for smaller bundle
  minify: true,
  // Keep readable names for debugging
  keepNames: true,
});

const bundle = readFileSync(distBundlePath, "utf-8").replace(/[ \t]+$/gm, "");
writeFileSync(distBundlePath, bundle);

// Copy bundle to Python package location
cpSync(distBundlePath, pyBundlePath);

// Generate package.json for npm publish
const pkgJson = {
  name: "@gobby/setup",
  version,
  description: "Interactive setup wizard for Gobby",
  type: "module",
  bin: { "gobby-setup": "./cli.mjs" },
  engines: { node: ">=18" },
  files: ["cli.mjs"],
  keywords: ["gobby", "setup", "cli", "ai", "coding"],
  license: "Apache-2.0",
  repository: {
    type: "git",
    url: "https://github.com/GobbyAI/gobby",
  },
};

writeFileSync(
  join(distSetup, "package.json"),
  JSON.stringify(pkgJson, null, 2) + "\n",
);

console.log(`Done. Outputs:`);
console.log(`  npm:    ${distBundlePath}`);
console.log(`  python: ${pyBundlePath}`);
console.log(`  pkg:    ${join(distSetup, "package.json")}`);
