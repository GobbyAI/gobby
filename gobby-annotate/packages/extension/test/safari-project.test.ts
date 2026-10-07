import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const project = readFileSync(
  fileURLToPath(
    new URL(
      "../../../safari/GobbyAnnotate.xcodeproj/project.pbxproj",
      import.meta.url,
    ),
  ),
  "utf8",
);

// Object definition body for a 24-hex pbxproj identifier.
function object(id: string) {
  const start = project.search(
    new RegExp(`\\n\\t\\t${id} /\\* [^*]+ \\*/ = \\{`),
  );
  expect(start).toBeGreaterThan(-1);
  return project.slice(start, project.indexOf("\n\t\t};", start));
}

function target(name: string) {
  const start = project.indexOf(
    `/* ${name} */ = {\n\t\t\tisa = PBXNativeTarget;`,
  );
  expect(start).toBeGreaterThan(-1);
  const body = project.slice(start, project.indexOf("\n\t\t};", start));
  const phases = [
    ...body
      .slice(body.indexOf("buildPhases = ("))
      .split(");")[0]
      .matchAll(/([0-9A-F]{24}) \/\* ([^*]+) \*\//g),
  ].map(([, id, label]) => ({ id, label }));
  return { phases, configs: configs(body) };
}

// Build configuration IDs of the target or project body's configuration list.
function configs(body: string) {
  const list = body.match(/buildConfigurationList = ([0-9A-F]{24})/)![1];
  return object(list)
    .split("buildConfigurations = (")[1]
    .split(");")[0]
    .match(/[0-9A-F]{24}/g)!;
}

describe("Safari Xcode project", () => {
  // The app copies dist/safari as plain resources. Without this phase an Xcode
  // build silently ships whatever bundle was last built, which is how a stale
  // October 2 content script reached the iPhone after later fixes landed.
  it.each(["GobbyAnnotate Extension (iOS)", "GobbyAnnotate Extension (macOS)"])(
    "%s builds the extension bundle before copying resources",
    (name) => {
      const { phases, configs } = target(name);
      const labels = phases.map((p) => p.label);
      expect(labels[0]).toBe("Build extension bundle");
      expect(labels).toContain("Resources");
      const script = object(phases[0].id);
      expect(script).toContain("isa = PBXShellScriptBuildPhase;");
      expect(script).toContain("npm run build:extension");
      expect(script).toContain("alwaysOutOfDate = 1;");
      expect(configs).toHaveLength(2);
      for (const id of configs)
        // The script writes dist/, which user-script sandboxing would deny.
        expect(object(id)).toContain("ENABLE_USER_SCRIPT_SANDBOXING = NO;");
    },
  );

  // Signing teams are personal: each developer supplies one through the
  // gitignored Signing.local.xcconfig that the project base config includes.
  it("commits no signing team and reads it from the local xcconfig", () => {
    expect(project).not.toContain("DEVELOPMENT_TEAM");
    const ids = configs(object("0381B5D53054E7B300E7B608"));
    expect(ids).toHaveLength(2);
    for (const id of ids)
      expect(object(id)).toMatch(
        /baseConfigurationReference = [0-9A-F]{24} \/\* Signing\.xcconfig \*\/;/,
      );
    const base = readFileSync(
      fileURLToPath(
        new URL("../../../safari/Signing.xcconfig", import.meta.url),
      ),
      "utf8",
    );
    expect(base).toContain('#include? "Signing.local.xcconfig"');
  });
});
