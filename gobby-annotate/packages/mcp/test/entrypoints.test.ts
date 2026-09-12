import { mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { expect, test } from "vitest";

test.each(["cli", "server"])(
  "%s runs through an npm-style executable symlink",
  async (entry) => {
    const directory = await mkdtemp(join(tmpdir(), "annotate-bin-"));
    try {
      const bin = join(directory, "annotate");
      await symlink(resolve(`packages/mcp/dist/${entry}.js`), bin);
      const result = spawnSync(bin, [], { encoding: "utf8", timeout: 10_000 });
      expect(result.error).toBeUndefined();
      expect(result.status).toBe(1);
      expect(result.stdout).toBe("");
      expect(result.stderr).toContain("Usage: gobby-annotate");
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  },
);
