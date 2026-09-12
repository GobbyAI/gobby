import { afterEach, expect, it } from "vitest";
import { mkdtemp, writeFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { encodeBundle } from "@gobby/annotate-core";
import { fixture, png } from "../../core/test/fixtures";
import { Catalog } from "../src/catalog";
const dirs: string[] = [];
afterEach(async () => {
  for (const dir of dirs.splice(0))
    await rm(dir, { recursive: true, force: true });
});
async function root() {
  const dir = await mkdtemp(join(tmpdir(), "annotate-"));
  dirs.push(dir);
  return dir;
}
it("serves a real stdio client, refreshes exports and returns original image content", async () => {
  const dir = await root();
  const client = new Client({ name: "test", version: "1" });
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [resolve("packages/mcp/dist/server.js"), "--root", dir],
  });
  try {
    await client.connect(transport);
    expect((await client.listTools()).tools.map((t) => t.name)).toEqual([
      "list_captures",
      "get_capture",
      "get_annotation",
      "get_screenshot",
    ]);
    expect(
      JSON.stringify(
        await client.callTool({ name: "list_captures", arguments: {} }),
      ),
    ).toContain("captures");
    const bundle = fixture();
    await writeFile(join(dir, "review.zip"), encodeBundle(bundle));
    expect(
      JSON.stringify(
        await client.callTool({ name: "list_captures", arguments: {} }),
      ),
    ).toContain(bundle.manifest.exportId);
    const args = {
      export_id: bundle.manifest.exportId,
      annotation_id: bundle.manifest.annotations[0]!.id,
    };
    const annotation = await client.callTool({
      name: "get_annotation",
      arguments: args,
    });
    expect(JSON.stringify(annotation)).toContain("Increase button contrast");
    expect(JSON.stringify(annotation)).not.toContain(
      Buffer.from(png).toString("base64"),
    );
    expect(
      (await client.callTool({ name: "get_screenshot", arguments: args }))
        .content,
    ).toEqual([
      {
        type: "image",
        mimeType: "image/png",
        data: Buffer.from(png).toString("base64"),
      },
    ]);
  } finally {
    await client.close();
  }
});
it("reports invalid captures and rejects symlink/path escapes", async () => {
  const dir = await root(),
    other = await root();
  const catalog = new Catalog(dir);
  await writeFile(join(dir, "invalid.zip"), "bad");
  await writeFile(join(other, "outside.zip"), encodeBundle(fixture()));
  await symlink(join(other, "outside.zip"), join(dir, "escape.zip"));
  expect((await catalog.discover()).errors).toHaveLength(2);
  await expect(catalog.contained("../outside.zip")).rejects.toThrow(/escapes/);
  await expect(catalog.get(fixture().manifest.exportId)).rejects.toThrow(
    /not found/,
  );
});

it("rejects an export replaced between discovery and retrieval", async () => {
  const dir = await root();
  const path = join(dir, "review.zip");
  const original = fixture();
  const replacement = fixture();
  replacement.manifest.exportId = "30000000-0000-4000-8000-000000000002";
  await writeFile(path, encodeBundle(original));
  class ReplacingCatalog extends Catalog {
    override async discover() {
      const inventory = await super.discover();
      await writeFile(path, encodeBundle(replacement));
      return inventory;
    }
  }
  await expect(
    new ReplacingCatalog(dir).get(original.manifest.exportId),
  ).rejects.toThrow(/Capture changed during read/);
});
