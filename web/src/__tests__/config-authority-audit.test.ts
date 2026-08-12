import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it, vi } from "vitest";

import { ConfigurationClient } from "../api/config";

const SRC_ROOT = join(process.cwd(), "src");
const AUTHORITY = "api/config.ts";

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) {
      return name === "__tests__" ? [] : sourceFiles(path);
    }
    return /\.(?:ts|tsx)$/.test(name) ? [path] : [];
  });
}

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}

describe("browser configuration authority", () => {
  it("web_has_one_config_authority", () => {
    const violations: string[] = [];
    for (const path of sourceFiles(SRC_ROOT)) {
      const relativePath = relative(SRC_ROOT, path);
      if (relativePath === AUTHORITY) continue;
      const source = stripComments(readFileSync(path, "utf8"));
      if (/fetch\s*\([\s\S]{0,300}\/api\/config\//.test(source)) {
        violations.push(`${relativePath}: direct configuration fetch`);
      }
      if (
        /\/api\/config\/(?:ui-settings|values\/reset|launch-defaults?)/.test(
          source,
        )
      ) {
        violations.push(`${relativePath}: specialized configuration endpoint`);
      }
    }

    expect(violations).toEqual([]);
  });

  // Behavioral counterpart to the structural scan above: the authority's
  // mutation path must actually carry the current revision as an optimistic
  // concurrency guard, not merely contain the right-looking source text.
  it("authority_patches_carry_the_current_revision", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ url: String(input), init });
        if (init?.method === "PATCH") {
          return new Response(
            JSON.stringify({
              committed: true,
              revision: 8,
              changed_keys: [],
              apply_status: "applied",
              pending_restart_keys: [],
              failed_live_keys: {},
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            revision: 7,
            desired: {},
            active: {},
            secret_set: {},
            pending_restart_keys: [],
            failed_live_keys: {},
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();

    await client.patch({ ui_settings: { theme: "light" } });

    const mutation = requests.find(
      (request) => request.init?.method === "PATCH",
    );
    expect(mutation?.url).toBe("/api/config/values");
    expect(JSON.parse(String(mutation?.init?.body))).toMatchObject({
      expected_revision: 7,
      values: { ui_settings: { theme: "light" } },
    });
  });
});
