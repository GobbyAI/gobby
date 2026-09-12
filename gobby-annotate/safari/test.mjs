import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { build } from "esbuild";
import assert from "node:assert/strict";

const source = dirname(fileURLToPath(import.meta.url));
const developer =
  process.env.DEVELOPER_DIR ?? "/Applications/Xcode.app/Contents/Developer";
const platform = join(developer, "Platforms/MacOSX.platform/Developer");
const frameworks = join(platform, "Library/Frameworks");
const libraries = join(platform, "usr/lib");
const temporary = await mkdtemp(join(tmpdir(), "annotate-native-tests-"));
try {
  const binary = join(temporary, "ExportStoreTests");
  const fixtureModule = join(temporary, "fixture.mjs");
  await build({
    stdin: {
      contents:
        'export { fixture } from "./packages/core/test/fixtures.ts"; export { encodeBundle, decodeBundle } from "./packages/core/src/index.ts";',
      resolveDir: dirname(source),
    },
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: fixtureModule,
  });
  const { fixture, encodeBundle, decodeBundle } = await import(
    pathToFileURL(fixtureModule).href
  );
  const bundle = fixture();
  bundle.manifest.annotations = Array.from({ length: 20 }, () => ({
    ...bundle.manifest.annotations[0],
    id: crypto.randomUUID(),
    comment: "x".repeat(20_000),
  }));
  const input = join(temporary, "input.zip"),
    output = join(temporary, "output.zip");
  const encoded = encodeBundle(bundle);
  await writeFile(input, encoded);
  const env = {
    ...process.env,
    DEVELOPER_DIR: developer,
    ANNOTATE_TEST_BUNDLE: input,
    ANNOTATE_TEST_RESULT: output,
  };
  execFileSync(
    "xcrun",
    [
      "swiftc",
      "-I",
      libraries,
      "-L",
      libraries,
      "-F",
      frameworks,
      "-Xlinker",
      "-rpath",
      "-Xlinker",
      frameworks,
      "-Xlinker",
      "-rpath",
      "-Xlinker",
      libraries,
      "-o",
      binary,
      join(source, "ExportStore.swift"),
      join(source, "ExportStoreTests.swift"),
    ],
    { env, stdio: "inherit" },
  );
  execFileSync(binary, [], {
    stdio: "inherit",
    env: {
      ...env,
      DYLD_FRAMEWORK_PATH: [
        join(platform, "Library/PrivateFrameworks"),
        join(developer, "../SharedFrameworks"),
      ].join(":"),
    },
  });
  const transferred = new Uint8Array(await readFile(output));
  assert.deepEqual(transferred, encoded);
  assert.deepEqual(decodeBundle(transferred), bundle);
} finally {
  await rm(temporary, { recursive: true, force: true });
}
