import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { pathToFileURL } from "node:url";
import { realpathSync } from "node:fs";
import { id } from "@gobby/annotate-core";
import { Catalog } from "./catalog";

const text = (value: unknown) => ({
  content: [{ type: "text" as const, text: JSON.stringify(value) }],
});
export function createServer(root: string): McpServer {
  const catalog = new Catalog(root);
  const server = new McpServer({ name: "gobby-annotate", version: "0.1.0" });
  const annotations = {
    readOnlyHint: true,
    destructiveHint: false,
    idempotentHint: true,
    openWorldHint: false,
  };
  server.registerTool(
    "list_captures",
    {
      description: "Discover immutable exports and report invalid bundles.",
      inputSchema: {},
      annotations,
    },
    async () => text(await catalog.discover()),
  );
  server.registerTool(
    "get_capture",
    {
      description:
        "Read capture metadata and annotations, without image bytes.",
      inputSchema: { export_id: id },
      annotations,
    },
    async ({ export_id }) => text((await catalog.get(export_id)).manifest),
  );
  server.registerTool(
    "get_annotation",
    {
      description: "Read one annotation and its evidence metadata.",
      inputSchema: { export_id: id, annotation_id: id },
      annotations,
    },
    async ({ export_id, annotation_id }) => {
      const a = (await catalog.get(export_id)).manifest.annotations.find(
        (a) => a.id === annotation_id,
      );
      if (!a) throw new Error(`Annotation not found: ${annotation_id}`);
      return text(a);
    },
  );
  server.registerTool(
    "get_screenshot",
    {
      description: "Retrieve original PNG image content on explicit request.",
      inputSchema: { export_id: id, annotation_id: id },
      annotations,
    },
    async ({ export_id, annotation_id }) => {
      const bundle = await catalog.get(export_id);
      const a = bundle.manifest.annotations.find((a) => a.id === annotation_id);
      if (!a) throw new Error(`Annotation not found: ${annotation_id}`);
      if (a.screenshot.status === "unavailable") return text(a.screenshot);
      return {
        content: [
          {
            type: "image" as const,
            mimeType: "image/png",
            data: Buffer.from(bundle.assets.get(a.screenshot.path)!).toString(
              "base64",
            ),
          },
        ],
      };
    },
  );
  return server;
}

export async function main(args = process.argv.slice(2)): Promise<void> {
  if (args.length !== 2 || args[0] !== "--root" || !args[1])
    throw new Error("Usage: gobby-annotate-mcp --root <directory>");
  await new Catalog(args[1]).validateRoot();
  await createServer(args[1]).connect(new StdioServerTransport());
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href
) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
