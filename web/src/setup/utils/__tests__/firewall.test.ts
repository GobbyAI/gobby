import { mkdtempSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { pathToFileURL } from "url";
import { afterEach, describe, expect, it } from "vitest";
import { resolveFirewallScriptPath } from "../firewall.js";

const tempDirs: string[] = [];

afterEach(() => {
  for (const dir of tempDirs.splice(0)) {
    rmSync(dir, { recursive: true, force: true });
  }
});

describe("resolveFirewallScriptPath", () => {
  it("resolves the script beside the npx package bundle without GOBBY_INSTALL_DIR", () => {
    const packageDir = mkdtempSync(join(tmpdir(), "gobby-setup-"));
    tempDirs.push(packageDir);
    const scriptPath = join(packageDir, "setup-firewall.sh");
    writeFileSync(scriptPath, "#!/bin/bash\n");

    const bundleUrl = pathToFileURL(join(packageDir, "cli.mjs")).href;

    expect(resolveFirewallScriptPath(bundleUrl, undefined)).toBe(scriptPath);
  });

  it("returns null when no packaged or installed script exists", () => {
    const packageDir = mkdtempSync(join(tmpdir(), "gobby-setup-"));
    tempDirs.push(packageDir);
    const bundleUrl = pathToFileURL(join(packageDir, "cli.mjs")).href;

    expect(resolveFirewallScriptPath(bundleUrl, undefined)).toBeNull();
  });
});
