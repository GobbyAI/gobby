import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { readFile, readdir, writeFile } from "node:fs/promises";

// Copy upstream notices for bundled runtime dependencies, including their closure.
export async function writeNotices(names, output) {
  const seen = new Set();
  const sections = [];
  async function visit(name, from = import.meta.url) {
    if (name.startsWith("@gobby/")) return;
    const require = createRequire(from);
    let root;
    try {
      root = dirname(require.resolve(`${name}/package.json`));
    } catch (error) {
      if (error.code !== "ERR_PACKAGE_PATH_NOT_EXPORTED") throw error;
      root = dirname(require.resolve(name));
      while (
        JSON.parse(
          await readFile(join(root, "package.json"), "utf8").catch((error) => {
            if (error.code === "ENOENT") return "{}";
            throw error;
          }),
        ).name !== name
      ) {
        const parent = dirname(root);
        if (parent === root)
          throw new Error(`Cannot locate package metadata: ${name}`, {
            cause: error,
          });
        root = parent;
      }
    }
    const metadata = JSON.parse(
      await readFile(join(root, "package.json"), "utf8"),
    );
    const identity = `${metadata.name}@${metadata.version}`;
    if (seen.has(identity)) return;
    seen.add(identity);
    const files = (await readdir(root))
      .filter((file) => /^(licen[cs]e|ofl|notice)(\.|$)/i.test(file))
      .sort();
    if (!files.length)
      throw new Error(`No upstream license file found: ${identity}`);
    for (const file of files)
      sections.push(
        `${identity} — ${file}\n\n${await readFile(join(root, file), "utf8")}`,
      );
    for (const dependency of Object.keys(metadata.dependencies ?? {}).sort())
      await visit(dependency, join(root, "package.json"));
  }
  for (const name of names.sort()) await visit(name);
  await writeFile(
    output,
    sections.join("\n\n----------------------------------------\n\n"),
  );
}
