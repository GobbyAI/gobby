import { expect, it } from "vitest";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { encodeBundle } from "@gobby/annotate-core";
import { fixture, png } from "../../core/test/fixtures";
import { readCapture } from "../src/catalog";
import { run } from "../src/cli";
it("inspects without binary and extracts validated assets without overwriting", async () => {
  const dir = await mkdtemp(join(tmpdir(), "annotate-cli-"));
  try {
    const path = join(dir, "input.zip"),
      to = join(dir, "output");
    await writeFile(path, encodeBundle(fixture()));
    const text = await run(["inspect", path]);
    expect(JSON.parse(text)).toEqual(fixture().manifest);
    expect(text).not.toContain(Buffer.from(png).toString("base64"));
    await run(["extract", path, "--to", to]);
    expect(await readCapture(to)).toEqual(fixture());
    await expect(run(["extract", path, "--to", to])).rejects.toThrow(
      /already exists/,
    );
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

it("allows only one concurrent extraction to reserve a destination", async () => {
  const dir = await mkdtemp(join(tmpdir(), "annotate-cli-race-"));
  try {
    const path = join(dir, "input.zip"),
      to = join(dir, "output");
    await writeFile(path, encodeBundle(fixture()));
    const results = await Promise.allSettled([
      run(["extract", path, "--to", to]),
      run(["extract", path, "--to", to]),
    ]);
    expect(
      results.filter((result) => result.status === "fulfilled"),
    ).toHaveLength(1);
    const rejected = results.find((result) => result.status === "rejected");
    expect(rejected?.reason).toBeInstanceOf(Error);
    expect(String(rejected?.reason)).toContain("already exists");
    expect(await readCapture(to)).toEqual(fixture());
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
