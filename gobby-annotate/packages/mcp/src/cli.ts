import { mkdir, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { realpathSync } from "node:fs";
import { readCapture } from "./catalog";

export async function run(args: string[]): Promise<string> {
  const [command, capture, flag, destination] = args;
  if (
    !capture ||
    !["inspect", "extract"].includes(command ?? "") ||
    (command === "inspect" && args.length !== 2) ||
    (command === "extract" &&
      (args.length !== 4 || flag !== "--to" || !destination))
  )
    throw new Error(
      "Usage: gobby-annotate inspect <capture> | extract <capture> --to <new-directory>",
    );
  const bundle = await readCapture(resolve(capture));
  if (command === "inspect") return JSON.stringify(bundle.manifest, null, 2);
  const to = resolve(destination!);
  try {
    await mkdir(to, { mode: 0o700 });
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "EEXIST")
      throw new Error(
        "Extraction destination already exists; choose a new directory",
        { cause: error },
      );
    throw error;
  }
  try {
    if (bundle.assets.size) await mkdir(join(to, "screenshots"));
    for (const [name, bytes] of bundle.assets)
      await writeFile(join(to, name), bytes, { flag: "wx" });
    // Publish the manifest last so interrupted extraction is not a valid capture.
    await writeFile(
      join(to, "capture.json"),
      JSON.stringify(bundle.manifest, null, 2),
      { flag: "wx" },
    );
    return `Extracted ${bundle.manifest.exportId} to ${to}`;
  } catch (error) {
    await rm(to, { recursive: true, force: true });
    throw error;
  }
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href
) {
  run(process.argv.slice(2))
    .then((output) => console.log(output))
    .catch((error) => {
      console.error(error instanceof Error ? error.message : error);
      process.exitCode = 1;
    });
}
