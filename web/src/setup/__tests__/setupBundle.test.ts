import { readFileSync } from "fs";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";

describe("setup bundle FalkorDB contract", () => {
  it("is regenerated with FalkorDB flags and state fields", () => {
    const testDir = dirname(fileURLToPath(import.meta.url));
    const bundlePath = resolve(
      testDir,
      "../../../../src/gobby/install/shared/setup/setup.mjs",
    );
    const bundle = readFileSync(bundlePath, "utf-8");

    expect({
      hasFalkorFlag: bundle.includes("--falkordb"),
      hasFalkorPasswordFlag: bundle.includes("--falkordb-password"),
      hasFalkorInstalled: bundle.includes("falkordb_installed"),
      hasFalkorPasswordSet: bundle.includes("falkordb_password_set"),
      hasNeo4jFlag: bundle.includes("--neo4j"),
      hasNeo4jPasswordFlag: bundle.includes("--neo4j-password"),
      hasNeo4jInstalledMigrator: bundle.includes("neo4j_installed"),
      hasNeo4jPasswordSetMigrator: bundle.includes("neo4j_password_set"),
    }).toEqual({
      hasFalkorFlag: true,
      hasFalkorPasswordFlag: true,
      hasFalkorInstalled: true,
      hasFalkorPasswordSet: true,
      hasNeo4jFlag: false,
      hasNeo4jPasswordFlag: false,
      hasNeo4jInstalledMigrator: true,
      hasNeo4jPasswordSetMigrator: true,
    });
  });
});
