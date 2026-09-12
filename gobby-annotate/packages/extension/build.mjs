import { build as viteBuild } from "vite";
import { build } from "esbuild";
import { copyFile, mkdir, writeFile, readFile } from "node:fs/promises";
import { writeNotices } from "../../notices.mjs";
import { manifest } from "./manifest.ts";
await mkdir("dist/public", { recursive: true });
for (const file of ["logo.png", "logo-light.png"])
  await copyFile("../../../web/public/" + file, "dist/public/" + file);
await viteBuild();
await build({
  entryPoints: ["src/background.ts"],
  bundle: true,
  platform: "browser",
  format: "esm",
  target: "safari17",
  outfile: "dist/chrome/background.js",
});
await writeFile("dist/chrome/manifest.json", JSON.stringify(manifest, null, 2));
await copyFile("../../../LICENSE", "dist/chrome/LICENSE");
const metadata = JSON.parse(await readFile("package.json", "utf8"));
await writeNotices(
  [...Object.keys(metadata.dependencies), "fflate", "zod", "tailwindcss"],
  "dist/chrome/THIRD_PARTY_NOTICES.txt",
);
await mkdir("dist/safari", { recursive: true });
for (const file of [
  "content.js",
  "background.js",
  "LICENSE",
  "THIRD_PARTY_NOTICES.txt",
  "logo.png",
  "logo-light.png",
])
  await copyFile("dist/chrome/" + file, "dist/safari/" + file);
await writeFile(
  "dist/safari/manifest.json",
  JSON.stringify(
    {
      ...manifest,
      permissions: ["activeTab", "scripting", "storage", "nativeMessaging"],
      background: { scripts: ["background.js"] },
    },
    null,
    2,
  ),
);
