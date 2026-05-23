import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadState } from "../state.js";

describe("setup state FalkorDB migration", () => {
  let gobbyHome: string;
  const originalGobbyHome = process.env.GOBBY_HOME;

  beforeEach(() => {
    gobbyHome = mkdtempSync(join(tmpdir(), "gobby-setup-state-"));
    process.env.GOBBY_HOME = gobbyHome;
  });

  afterEach(() => {
    process.env.GOBBY_HOME = originalGobbyHome;
    rmSync(gobbyHome, { recursive: true, force: true });
  });

  it("renames persisted neo4j fields to falkordb fields and writes the migrated state once", () => {
    const stateFile = join(gobbyHome, "setup_state.json");
    writeFileSync(
      stateFile,
      JSON.stringify({
        version: 2,
        started_at: "2026-05-22T00:00:00.000Z",
        completed_at: null,
        completed_step_id: "services",
        neo4j_installed: true,
        neo4j_password_set: true,
      }),
    );

    const loaded = loadState() as unknown as Record<string, unknown>;

    expect(loaded).toMatchObject({
      version: 3,
      falkordb_installed: false,
      falkordb_password_set: false,
    });
    expect(loaded).not.toHaveProperty("neo4j_installed");
    expect(loaded).not.toHaveProperty("neo4j_password_set");

    const persisted = JSON.parse(readFileSync(stateFile, "utf-8")) as Record<string, unknown>;
    expect(persisted).toMatchObject({
      version: 3,
      falkordb_installed: false,
      falkordb_password_set: false,
    });
    expect(persisted).not.toHaveProperty("neo4j_installed");
    expect(persisted).not.toHaveProperty("neo4j_password_set");
  });
});
