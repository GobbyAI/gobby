import { build } from "esbuild";
import { copyFile, chmod } from "node:fs/promises";
import { writeNotices } from "../../notices.mjs";
await build({
  entryPoints: ["src/server.ts", "src/cli.ts"],
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node22",
  outdir: "dist",
  external: ["@modelcontextprotocol/sdk/*"],
  banner: { js: "#!/usr/bin/env node" },
});
await copyFile("../../../LICENSE", "LICENSE");
await writeNotices(["fflate", "zod"], "dist/THIRD_PARTY_NOTICES.txt");
for (const file of ["dist/server.js", "dist/cli.js"]) await chmod(file, 0o755);
