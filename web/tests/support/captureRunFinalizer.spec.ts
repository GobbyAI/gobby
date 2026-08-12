/**
 * Node-side coverage for the capture-run coordinator (`captureRunFinalizer.ts`)
 * under the Playwright runner — temp staging trees, no browser.
 *
 * Covers: matrix-expansion correctness, missing cell, duplicate success,
 * failed-then-successful retry, interrupted finalization, concurrent cell
 * completion, inactive-run no-op with staged files present, stale-staging
 * refusal, foreign-key rejection, overwrite refusal, and — via a child
 * Playwright run against the real reporter — proof that body-pass/hook-fail
 * and body-pass/fixture-teardown-fail attempts yield no success attestation.
 */

import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { ACTIVITY_PANEL_TABS } from "../../src/components/activity/ActivityPanelTabs";
import { SETTINGS_SECTIONS } from "../../src/components/settings/sections";
import captureRunGlobalTeardown, {
  CAPTURE_POINTERS,
  CAPTURE_ROOT_ENV,
  CAPTURE_RUN_ENV,
  CAPTURE_THEMES,
  CAPTURE_VIEWPORTS,
  type CaptureCellFragment,
  expandCaptureCells,
  expandCaptureCellKeys,
  finalizeCaptureRun,
  runDirFor,
  sha256Hex,
  stagingDirFor,
} from "./captureRunFinalizer";

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(MODULE_DIR, "..", "..");

/** Hand-stage one attempt of one cell, bypassing the live helper so tests
 * control attestation, hashes, and SHAs directly. */
function stageAttempt(
  rootDir: string,
  runId: string,
  cellKey: string,
  options: {
    attempt: number;
    attested: boolean;
    png?: Buffer;
    gitSha?: string;
    recordedPngSha?: string;
  },
): Buffer {
  const png = options.png ?? Buffer.from(`png:${cellKey}:${options.attempt}`);
  const attemptDir = path.join(
    stagingDirFor(rootDir, runId),
    cellKey,
    `attempt-${options.attempt}`,
  );
  fs.mkdirSync(attemptDir, { recursive: true });
  const fragment: CaptureCellFragment = {
    runId,
    cellKey,
    scenario: cellKey.split("--")[0] ?? cellKey,
    state: "base",
    stateVariant: "base",
    theme: "dark",
    pointer: "fine",
    viewport: "desktop",
    planSection: "1.3",
    gitSha: options.gitSha ?? "feedc0de",
    attempt: options.attempt,
    pngSha256: options.recordedPngSha ?? sha256Hex(png),
  };
  fs.writeFileSync(path.join(attemptDir, "capture.png"), png);
  fs.writeFileSync(
    path.join(attemptDir, "fragment.json"),
    JSON.stringify(fragment, null, 2),
  );
  if (options.attested) {
    fs.writeFileSync(
      path.join(attemptDir, "attested.json"),
      JSON.stringify({
        runId,
        cellKey,
        attempt: options.attempt,
        status: "passed",
      }),
    );
  }
  return png;
}

const TINY_KEYS = [
  "alpha--base--dark--fine--desktop",
  "beta--base--dark--fine--desktop",
];

test.describe("matrix expansion", () => {
  test("derives the roster from the live registries", () => {
    const cells = expandCaptureCells();
    const keys = expandCaptureCellKeys();
    expect(keys).toEqual(cells.map((cell) => cell.key));
    expect(new Set(keys).size).toBe(keys.length);
    expect([...keys].sort((a, b) => a.localeCompare(b))).toEqual(keys);

    const scenarios = new Set(cells.map((cell) => cell.scenario));

    // One scenario per live activity tab, derived — not hardcoded.
    for (const tab of ACTIVITY_PANEL_TABS) {
      expect(scenarios.has(`tab-${tab.id}`)).toBe(true);
    }
    expect([...scenarios].filter((id) => id.startsWith("tab-")).length).toBe(
      ACTIVITY_PANEL_TABS.length,
    );

    // One settings scenario per live settings section, derived.
    for (const section of SETTINGS_SECTIONS) {
      expect(scenarios.has(`settings-${section.id}`)).toBe(true);
    }
    expect(
      [...scenarios].filter((id) => id.startsWith("settings-")).length,
    ).toBe(SETTINGS_SECTIONS.length);

    // The non-registry base scenarios.
    for (const id of [
      "login",
      "chat",
      "composer",
      "agents-editor",
      "memory-graph",
      "mobile-toolbar",
    ]) {
      expect(scenarios.has(id)).toBe(true);
    }

    // Base states expand across the full matrix.
    const fullMatrix =
      CAPTURE_THEMES.length *
      CAPTURE_POINTERS.length *
      CAPTURE_VIEWPORTS.length;
    const loginCells = cells.filter((cell) => cell.scenario === "login");
    expect(loginCells.length).toBe(fullMatrix);

    // Grayscale subset: state-bearing rows in both themes.
    const grayCells = cells.filter((cell) => cell.grayscale);
    expect(grayCells.length).toBeGreaterThan(0);
    for (const theme of CAPTURE_THEMES) {
      expect(
        grayCells.some(
          (cell) => cell.theme === theme && cell.scenario === "tab-tasks",
        ),
      ).toBe(true);
    }

    // Reduced-motion pairs: every family has a reduce cell and a
    // no-preference control.
    const rmCells = cells.filter((cell) => cell.motion !== null);
    const rmStates = new Set(
      rmCells.map((cell) => `${cell.scenario}:${cell.state}`),
    );
    expect(rmStates).toEqual(
      new Set([
        "chat:streaming",
        "chat:loading",
        "composer:voice-recording",
        "composer:voice-listening",
      ]),
    );
    for (const state of rmStates) {
      const pair = rmCells.filter(
        (cell) => `${cell.scenario}:${cell.state}` === state,
      );
      expect(pair.map((cell) => cell.motion).sort()).toEqual([
        "none",
        "reduce",
      ]);
    }

    // Key format is the stable 5-segment pairing name.
    for (const key of keys) {
      expect(key).toMatch(
        /^[a-z0-9-]+--[a-z0-9-]+(~[a-z0-9-]+)?--(dark|light)--(fine|coarse)--(desktop|portrait|landscape)$/,
      );
    }
  });
});

test.describe("finalizeCaptureRun", () => {
  test("publishes a complete run atomically and clears staging", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    const pngs = TINY_KEYS.map((key) =>
      stageAttempt(root, "run-a", key, { attempt: 0, attested: true }),
    );

    const manifest = finalizeCaptureRun({
      runId: "run-a",
      rootDir: root,
      expectedKeys: TINY_KEYS,
    });

    expect(manifest.cellCount).toBe(2);
    expect(manifest.gitSha).toBe("feedc0de");
    const runDir = runDirFor(root, "run-a");
    for (const [index, key] of TINY_KEYS.entries()) {
      const published = fs.readFileSync(path.join(runDir, `${key}.png`));
      expect(published.equals(pngs[index] ?? Buffer.alloc(0))).toBe(true);
      expect(manifest.cells[key]?.pngSha256).toBe(sha256Hex(published));
    }
    const onDisk = JSON.parse(
      fs.readFileSync(path.join(runDir, "run-manifest.json"), "utf8"),
    );
    expect(onDisk).toEqual(manifest);
    // Staging is scratch once the label is occupied.
    expect(fs.existsSync(stagingDirFor(root, "run-a"))).toBe(false);
  });

  test("a missing cell aborts publication with a diagnostic naming it", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    stageAttempt(root, "run-b", TINY_KEYS[0]!, { attempt: 0, attested: true });

    expect(() =>
      finalizeCaptureRun({
        runId: "run-b",
        rootDir: root,
        expectedKeys: TINY_KEYS,
      }),
    ).toThrow(TINY_KEYS[1]!);
    expect(fs.existsSync(runDirFor(root, "run-b"))).toBe(false);
    // Staging survives an aborted finalization — the label was never occupied.
    expect(fs.existsSync(stagingDirFor(root, "run-b"))).toBe(true);
  });

  test("an unknown staged key aborts as foreign work", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    for (const key of TINY_KEYS) {
      stageAttempt(root, "run-c", key, { attempt: 0, attested: true });
    }
    stageAttempt(root, "run-c", "intruder--base--dark--fine--desktop", {
      attempt: 0,
      attested: true,
    });

    expect(() =>
      finalizeCaptureRun({
        runId: "run-c",
        rootDir: root,
        expectedKeys: TINY_KEYS,
      }),
    ).toThrow(/foreign work.*intruder--base--dark--fine--desktop/s);
    expect(fs.existsSync(runDirFor(root, "run-c"))).toBe(false);
  });

  test("duplicate successful fragments resolve to the highest attempt", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    stageAttempt(root, "run-d", TINY_KEYS[0]!, { attempt: 0, attested: true });
    const winner = stageAttempt(root, "run-d", TINY_KEYS[0]!, {
      attempt: 2,
      attested: true,
    });
    stageAttempt(root, "run-d", TINY_KEYS[1]!, { attempt: 0, attested: true });

    const manifest = finalizeCaptureRun({
      runId: "run-d",
      rootDir: root,
      expectedKeys: TINY_KEYS,
    });

    expect(manifest.cells[TINY_KEYS[0]!]?.attempt).toBe(2);
    const published = fs.readFileSync(
      path.join(runDirFor(root, "run-d"), `${TINY_KEYS[0]!}.png`),
    );
    expect(published.equals(winner)).toBe(true);
  });

  test("a failed attempt followed by a successful retry publishes the retry", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    // Attempt 0: staged but never attested (the runner-final result failed).
    stageAttempt(root, "run-e", TINY_KEYS[0]!, { attempt: 0, attested: false });
    const retry = stageAttempt(root, "run-e", TINY_KEYS[0]!, {
      attempt: 1,
      attested: true,
    });
    stageAttempt(root, "run-e", TINY_KEYS[1]!, { attempt: 0, attested: true });

    const manifest = finalizeCaptureRun({
      runId: "run-e",
      rootDir: root,
      expectedKeys: TINY_KEYS,
    });

    expect(manifest.cells[TINY_KEYS[0]!]?.attempt).toBe(1);
    const published = fs.readFileSync(
      path.join(runDirFor(root, "run-e"), `${TINY_KEYS[0]!}.png`),
    );
    expect(published.equals(retry)).toBe(true);
  });

  test("a cell with only unattested attempts counts as missing", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    stageAttempt(root, "run-f", TINY_KEYS[0]!, { attempt: 0, attested: true });
    stageAttempt(root, "run-f", TINY_KEYS[1]!, { attempt: 0, attested: false });

    expect(() =>
      finalizeCaptureRun({
        runId: "run-f",
        rootDir: root,
        expectedKeys: TINY_KEYS,
      }),
    ).toThrow(TINY_KEYS[1]!);
  });

  test("a PNG that does not match its recorded hash aborts as corrupt", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    stageAttempt(root, "run-g", TINY_KEYS[0]!, { attempt: 0, attested: true });
    stageAttempt(root, "run-g", TINY_KEYS[1]!, {
      attempt: 0,
      attested: true,
      recordedPngSha: "0".repeat(64),
    });

    expect(() =>
      finalizeCaptureRun({
        runId: "run-g",
        rootDir: root,
        expectedKeys: TINY_KEYS,
      }),
    ).toThrow(/does not match its recorded hash/);
  });

  test("an interrupted finalization never occupies the label and recovery republishes", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    for (const key of TINY_KEYS) {
      stageAttempt(root, "run-h", key, { attempt: 0, attested: true });
    }
    // Simulate a crash mid-publish: an orphaned temp dir under runs/, label
    // never occupied.
    const runsDir = path.dirname(runDirFor(root, "run-h"));
    const orphan = path.join(runsDir, ".publish-run-h-dead");
    fs.mkdirSync(orphan, { recursive: true });
    fs.writeFileSync(path.join(orphan, "partial.png"), "partial");
    expect(fs.existsSync(runDirFor(root, "run-h"))).toBe(false);

    const manifest = finalizeCaptureRun({
      runId: "run-h",
      rootDir: root,
      expectedKeys: TINY_KEYS,
    });

    expect(manifest.cellCount).toBe(2);
    expect(fs.existsSync(orphan)).toBe(false);
    expect(fs.existsSync(runDirFor(root, "run-h"))).toBe(true);
  });

  test("concurrently completed cells from parallel workers all publish", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    const keys = Array.from(
      { length: 12 },
      (_, index) => `cell${index}--base--dark--fine--desktop`,
    );
    // Parallel workers write attempt dirs in arbitrary interleaved order;
    // staging isolation means order cannot matter.
    for (const key of [...keys].reverse()) {
      stageAttempt(root, "run-i", key, { attempt: 0, attested: true });
    }

    const manifest = finalizeCaptureRun({
      runId: "run-i",
      rootDir: root,
      expectedKeys: keys,
    });
    expect(manifest.cellCount).toBe(keys.length);
    expect(Object.keys(manifest.cells).length).toBe(keys.length);
  });

  test("mixed git SHAs in staging refuse as stale", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    stageAttempt(root, "run-j", TINY_KEYS[0]!, {
      attempt: 0,
      attested: true,
      gitSha: "aaaa1111",
    });
    stageAttempt(root, "run-j", TINY_KEYS[1]!, {
      attempt: 0,
      attested: true,
      gitSha: "bbbb2222",
    });

    expect(() =>
      finalizeCaptureRun({
        runId: "run-j",
        rootDir: root,
        expectedKeys: TINY_KEYS,
      }),
    ).toThrow(/stale/i);
  });

  test("a finalized run is immutable — overwrite refused, content untouched", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    for (const key of TINY_KEYS) {
      stageAttempt(root, "run-k", key, { attempt: 0, attested: true });
    }
    finalizeCaptureRun({
      runId: "run-k",
      rootDir: root,
      expectedKeys: TINY_KEYS,
    });
    const runDir = runDirFor(root, "run-k");
    const before = fs.readFileSync(
      path.join(runDir, "run-manifest.json"),
      "utf8",
    );

    // A second capture at the same label stages again and tries to publish.
    for (const key of TINY_KEYS) {
      stageAttempt(root, "run-k", key, {
        attempt: 0,
        attested: true,
        png: Buffer.from("different bytes"),
      });
    }
    expect(() =>
      finalizeCaptureRun({
        runId: "run-k",
        rootDir: root,
        expectedKeys: TINY_KEYS,
      }),
    ).toThrow(/already finalized/);
    expect(
      fs.readFileSync(path.join(runDir, "run-manifest.json"), "utf8"),
    ).toBe(before);
  });

  test("path-unsafe run labels are rejected", ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    expect(() =>
      finalizeCaptureRun({
        runId: "../escape",
        rootDir: root,
        expectedKeys: TINY_KEYS,
      }),
    ).toThrow(/Invalid capture run id/);
  });
});

test.describe("globalTeardown activation gate", () => {
  test("no active run id is a no-op even with staged files present", async ({}, testInfo) => {
    const root = testInfo.outputPath("root");
    // Stale staging left by an earlier aborted capture run.
    stageAttempt(root, "stale-run", TINY_KEYS[0]!, {
      attempt: 0,
      attested: true,
    });

    const savedRunId = process.env[CAPTURE_RUN_ENV];
    const savedRoot = process.env[CAPTURE_ROOT_ENV];
    delete process.env[CAPTURE_RUN_ENV];
    process.env[CAPTURE_ROOT_ENV] = root;
    try {
      await captureRunGlobalTeardown();
    } finally {
      if (savedRunId === undefined) {
        delete process.env[CAPTURE_RUN_ENV];
      } else {
        process.env[CAPTURE_RUN_ENV] = savedRunId;
      }
      if (savedRoot === undefined) {
        delete process.env[CAPTURE_ROOT_ENV];
      } else {
        process.env[CAPTURE_ROOT_ENV] = savedRoot;
      }
    }

    // Staging untouched, nothing published.
    expect(
      fs.existsSync(
        path.join(
          stagingDirFor(root, "stale-run"),
          TINY_KEYS[0]!,
          "attempt-0",
          "capture.png",
        ),
      ),
    ).toBe(true);
    expect(fs.existsSync(path.join(root, "runs"))).toBe(false);
  });
});

test.describe("runner-final success attestation (integration)", () => {
  // One child Playwright run against the real reporter proves the seam:
  // a clean pass is attested; a body-pass/afterEach-fail and a
  // body-pass/fixture-teardown-fail attempt are not.
  test("only runner-final passes receive attestations", ({}, testInfo) => {
    test.slow();
    const root = testInfo.outputPath("root");
    // The child project must live under web/ (node resolution for
    // @playwright/test) but outside any test-results directory, which
    // Playwright refuses to discover tests in. This root is gitignored.
    const childDir = path.join(
      MODULE_DIR,
      "..",
      "screenshots",
      "style-captures",
      ".attestation-it",
    );
    fs.rmSync(childDir, { recursive: true, force: true });
    fs.mkdirSync(childDir, { recursive: true });

    const finalizerPath = path
      .join(MODULE_DIR, "captureRunFinalizer.ts")
      .replaceAll("\\", "/");
    const reporterPath = path
      .join(MODULE_DIR, "captureRunReporter.ts")
      .replaceAll("\\", "/");

    fs.writeFileSync(
      path.join(childDir, "playwright.config.ts"),
      [
        `import { defineConfig } from "@playwright/test";`,
        `export default defineConfig({`,
        `  testDir: ".",`,
        `  workers: 1,`,
        `  retries: 0,`,
        `  reporter: [["list"], ["${reporterPath}"]],`,
        `});`,
        ``,
      ].join("\n"),
    );

    fs.writeFileSync(
      path.join(childDir, "attestation.spec.ts"),
      [
        `import { test as base } from "@playwright/test";`,
        `import { CAPTURE_VIEWPORTS, stageCaptureCell } from "${finalizerPath}";`,
        ``,
        `const cellFor = (name: string) => ({`,
        `  key: \`it-\${name}--base--dark--fine--desktop\`,`,
        `  scenario: \`it-\${name}\`,`,
        `  planSection: "1.3",`,
        `  state: "base",`,
        `  stateVariant: "base",`,
        `  theme: "dark" as const,`,
        `  pointer: "fine" as const,`,
        `  viewport: CAPTURE_VIEWPORTS[0],`,
        `  grayscale: false,`,
        `  motion: null,`,
        `});`,
        ``,
        `base("clean pass", async ({}, testInfo) => {`,
        `  stageCaptureCell(testInfo, cellFor("clean"), Buffer.from("png-clean"));`,
        `});`,
        ``,
        `base.describe("afterEach failure", () => {`,
        `  base.afterEach(() => {`,
        `    throw new Error("deliberate afterEach failure");`,
        `  });`,
        `  base("body passes", async ({}, testInfo) => {`,
        `    stageCaptureCell(testInfo, cellFor("hookfail"), Buffer.from("png-hook"));`,
        `  });`,
        `});`,
        ``,
        `const withFailingTeardown = base.extend<{ failing: void }>({`,
        `  failing: [`,
        `    async ({}, use) => {`,
        `      await use(undefined);`,
        `      throw new Error("deliberate fixture teardown failure");`,
        `    },`,
        `    { auto: true },`,
        `  ],`,
        `});`,
        `withFailingTeardown("body passes, fixture teardown fails", async ({}, testInfo) => {`,
        `  stageCaptureCell(testInfo, cellFor("fixfail"), Buffer.from("png-fix"));`,
        `});`,
        ``,
      ].join("\n"),
    );

    const playwrightBin = path.join(
      WEB_DIR,
      "node_modules",
      ".bin",
      process.platform === "win32" ? "playwright.cmd" : "playwright",
    );
    const runId = "attestation-it";
    try {
      execFileSync(playwrightBin, ["test", "--config", childDir], {
        cwd: WEB_DIR,
        env: {
          ...process.env,
          [CAPTURE_RUN_ENV]: runId,
          [CAPTURE_ROOT_ENV]: root,
          CI: "",
          PLAYWRIGHT_BASE_URL: "http://localhost:1",
        },
        stdio: "pipe",
      });
      throw new Error(
        "child playwright run should have failed (two tests fail by design)",
      );
    } catch (error) {
      if (!(error instanceof Error) || !("status" in error)) {
        throw error;
      }
    }

    const attestedPath = (name: string) =>
      path.join(
        stagingDirFor(root, runId),
        `it-${name}--base--dark--fine--desktop`,
        "attempt-0",
        "attested.json",
      );
    const stagedPath = (name: string) =>
      path.join(
        stagingDirFor(root, runId),
        `it-${name}--base--dark--fine--desktop`,
        "attempt-0",
        "fragment.json",
      );

    // All three attempts staged their fragments…
    expect(fs.existsSync(stagedPath("clean"))).toBe(true);
    expect(fs.existsSync(stagedPath("hookfail"))).toBe(true);
    expect(fs.existsSync(stagedPath("fixfail"))).toBe(true);
    // …but only the runner-final pass was attested.
    expect(fs.existsSync(attestedPath("clean"))).toBe(true);
    expect(fs.existsSync(attestedPath("hookfail"))).toBe(false);
    expect(fs.existsSync(attestedPath("fixfail"))).toBe(false);

    fs.rmSync(childDir, { recursive: true, force: true });
  });
});
