/**
 * Run-level coordinator for the style-surface capture harness.
 *
 * This module is the single source of truth for the capture matrix: the pure
 * expansion (`expandCaptureCells`) derives every expected cell from the live
 * `ACTIVITY_PANEL_TABS` and `SETTINGS_SECTIONS` registries, and both the
 * capture spec (`tests/style-surfaces.spec.ts`, to enumerate captures) and the
 * finalizer (to check completeness) consume it, so the two can never disagree.
 *
 * Lifecycle of a capture run (`GOBBY_CAPTURE_RUN_ID=<label>`):
 *
 *  1. Each matrix cell is one Playwright test. The test body stages its PNG
 *     and an immutable per-cell manifest fragment into an attempt-scoped
 *     staging directory via `stageCaptureCell` — never a success marker.
 *  2. `CaptureRunReporter.onTestEnd` — the runner-final seam, after
 *     `afterEach` hooks and fixture teardown have settled — writes the
 *     success attestation for attempts whose final result is `passed`. A
 *     body-passes/teardown-fails attempt therefore never carries one.
 *  3. The Playwright `globalTeardown` (default export) finalizes: it selects
 *     exactly one attested fragment per expected cell (highest attempt index
 *     wins across CI retries), requires exact expected-key-set equality
 *     (missing cell → abort naming it; unknown staged key → abort as foreign
 *     work; mixed git SHAs → abort as stale staging), then publishes the
 *     labeled run directory with a merged run manifest in one atomic rename.
 *     Overwrite refusal applies to finalized runs only — a failed or partial
 *     attempt never occupies the label.
 *
 * Without an active run id every entry point is a no-op, including against
 * stale staging left by an earlier aborted capture run.
 */

import { createHash } from "node:crypto";
import { execSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import type {
  FullConfig,
  Reporter,
  TestCase,
  TestResult,
} from "@playwright/test/reporter";
import type { TestInfo } from "@playwright/test";
import { ACTIVITY_PANEL_TABS } from "../../src/components/activity/ActivityPanelTabs";
import { SETTINGS_SECTIONS } from "../../src/components/settings/sections";

export const CAPTURE_RUN_ENV = "GOBBY_CAPTURE_RUN_ID";
export const CAPTURE_ROOT_ENV = "GOBBY_CAPTURE_ROOT";
export const CAPTURE_CELL_ATTACHMENT = "gobby-capture-cell";

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));

export const CAPTURE_THEMES = ["dark", "light"] as const;
export const CAPTURE_POINTERS = ["fine", "coarse"] as const;
export const CAPTURE_VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "portrait", width: 440, height: 956 },
  { name: "landscape", width: 932, height: 430 },
] as const;

export type CaptureTheme = (typeof CAPTURE_THEMES)[number];
export type CapturePointer = (typeof CAPTURE_POINTERS)[number];
export type CaptureViewportName = (typeof CAPTURE_VIEWPORTS)[number]["name"];
export type MotionPreference = "none" | "reduce";

export interface CaptureStateDescriptor {
  /** State id — the second segment of the cell key. */
  readonly id: string;
  /** Axis restrictions. Base states default to the full matrix; auxiliary
   * states default to both themes × fine pointer × desktop viewport. */
  readonly themes?: readonly CaptureTheme[];
  readonly pointers?: readonly CapturePointer[];
  readonly viewports?: readonly CaptureViewportName[];
  /** Also emit a desaturated `<id>~gray` variant in both themes at
   * desktop/fine — the repeatable deutan contract check. */
  readonly grayscale?: boolean;
  /** Emit a reduced-motion pair (`<id>~rm-reduce` + `<id>~rm-none` control)
   * instead of a plain cell — the executable reduced-motion contract. */
  readonly motionPair?: boolean;
}

export interface CaptureScenarioDescriptor {
  /** Surface id — the first segment of the cell key. */
  readonly id: string;
  /** Plan section this surface is evidence for (recorded in fragments). */
  readonly planSection: string;
  readonly states: readonly CaptureStateDescriptor[];
}

/** One expanded matrix cell — a single screenshot. */
export interface CaptureCell {
  readonly key: string;
  readonly scenario: string;
  readonly planSection: string;
  /** Base state id (without variant suffix). */
  readonly state: string;
  /** Full state segment as it appears in the key (with `~gray`/`~rm-*`). */
  readonly stateVariant: string;
  readonly theme: CaptureTheme;
  readonly pointer: CapturePointer;
  readonly viewport: (typeof CAPTURE_VIEWPORTS)[number];
  readonly grayscale: boolean;
  readonly motion: MotionPreference | null;
}

/** Plan sections each activity tab's capture is evidence for. */
const TAB_PLAN_SECTIONS: Record<string, string> = {
  sessions: "4.8",
  terminal: "4.9",
  tasks: "4.7",
  mcp: "4.8",
  agents: "4.2",
  stages: "4.8",
  skills: "4.8",
  memory: "4.8",
  integrations: "4.8",
  wiki: "4.4",
  rules: "4.8",
  plans: "4.8",
  changes: "4.8",
  files: "4.6",
  pipelines: "4.3",
  cron: "4.8",
};

/** Tabs whose rows carry task/session/pipeline-style status state and
 * therefore join the grayscale deutan subset. */
const STATE_BEARING_TABS = new Set([
  "sessions",
  "tasks",
  "pipelines",
  "agents",
  "stages",
  "cron",
]);

const FULL_MATRIX_STATE: CaptureStateDescriptor = { id: "base" };

/**
 * The surface-scenario manifest skeleton. Route/seed/checkpoint/readiness
 * implementations live in `tests/style-surfaces.spec.ts`, keyed by scenario
 * and state id; the spec asserts its implementation map covers exactly this
 * roster.
 */
export function buildCaptureScenarios(): CaptureScenarioDescriptor[] {
  const scenarios: CaptureScenarioDescriptor[] = [];

  scenarios.push({
    id: "login",
    planSection: "4.10",
    states: [FULL_MATRIX_STATE],
  });

  scenarios.push({
    id: "chat",
    planSection: "4.10",
    states: [
      { id: "base", grayscale: true },
      { id: "overflow" },
      { id: "stream-error", grayscale: true },
      { id: "streaming", motionPair: true },
      { id: "loading", motionPair: true },
    ],
  });

  scenarios.push({
    id: "composer",
    planSection: "5.2",
    states: [
      { id: "base" },
      { id: "voice-recording", motionPair: true },
      // The "speaking/listening" family: photographed via the VAD listening
      // status bar, the always-on member of the pair.
      { id: "voice-listening", motionPair: true },
    ],
  });

  for (const tab of ACTIVITY_PANEL_TABS) {
    const states: CaptureStateDescriptor[] = [
      { id: "base", grayscale: STATE_BEARING_TABS.has(tab.id) },
    ];
    if (tab.id === "sessions") {
      // The sessions surface owns the filter overlay; the activity chrome's
      // tab dropdown is photographed here as its host tab.
      states.push({ id: "filter-open" }, { id: "menu-open" });
    }
    if (tab.id === "wiki") {
      states.push({ id: "overflow" });
    }
    scenarios.push({
      id: `tab-${tab.id}`,
      planSection: TAB_PLAN_SECTIONS[tab.id] ?? "4.8",
      states,
    });
  }

  scenarios.push({
    id: "agents-editor",
    planSection: "4.1",
    states: [FULL_MATRIX_STATE],
  });

  scenarios.push({
    id: "memory-graph",
    planSection: "4.5",
    states: [
      // The graph opens on desktop-tier widths only (`useIsMobile` gates the
      // Show Graph action), so the portrait viewport cannot photograph it.
      { id: "base", viewports: ["desktop", "landscape"], pointers: CAPTURE_POINTERS },
    ],
  });

  for (const section of SETTINGS_SECTIONS) {
    scenarios.push({
      id: `settings-${section.id}`,
      planSection: "7.3",
      states: [FULL_MATRIX_STATE],
    });
  }

  scenarios.push({
    id: "mobile-toolbar",
    planSection: "1.4",
    states: [
      // Portrait only: today the mobile tier is width-gated (≤767px), so the
      // 932×430 landscape viewport renders the desktop toolbar. Landscape
      // mobile chrome arrives with 1.4's height clause and is re-baselined by
      // every base scenario's landscape cells.
      {
        id: "base",
        viewports: ["portrait"],
        pointers: ["fine", "coarse"],
      },
    ],
  });

  return scenarios;
}

function viewportByName(name: CaptureViewportName) {
  const viewport = CAPTURE_VIEWPORTS.find((entry) => entry.name === name);
  if (!viewport) {
    throw new Error(`Unknown capture viewport: ${name}`);
  }
  return viewport;
}

export function formatCaptureCellKey(cell: {
  scenario: string;
  stateVariant: string;
  theme: CaptureTheme;
  pointer: CapturePointer;
  viewport: { name: CaptureViewportName };
}): string {
  return [
    cell.scenario,
    cell.stateVariant,
    cell.theme,
    cell.pointer,
    cell.viewport.name,
  ].join("--");
}

/**
 * Pure matrix expansion: scenarios × themes × pointers × viewports plus the
 * grayscale, reduced-motion, and state-coverage cells. Deterministic and
 * sorted; the single roster consumed by both the spec and the finalizer.
 */
export function expandCaptureCells(
  scenarios: readonly CaptureScenarioDescriptor[] = buildCaptureScenarios(),
): CaptureCell[] {
  const cells: CaptureCell[] = [];

  for (const scenario of scenarios) {
    for (const state of scenario.states) {
      const isBase = state.id === "base";
      const themes = state.themes ?? CAPTURE_THEMES;
      const pointers =
        state.pointers ?? (isBase ? CAPTURE_POINTERS : (["fine"] as const));
      const viewportNames =
        state.viewports ??
        (isBase
          ? CAPTURE_VIEWPORTS.map((viewport) => viewport.name)
          : (["desktop"] as const));

      const push = (
        stateVariant: string,
        theme: CaptureTheme,
        pointer: CapturePointer,
        viewportName: CaptureViewportName,
        grayscale: boolean,
        motion: MotionPreference | null,
      ) => {
        const viewport = viewportByName(viewportName);
        const partial = {
          scenario: scenario.id,
          stateVariant,
          theme,
          pointer,
          viewport,
        };
        cells.push({
          key: formatCaptureCellKey(partial),
          scenario: scenario.id,
          planSection: scenario.planSection,
          state: state.id,
          stateVariant,
          theme,
          pointer,
          viewport,
          grayscale,
          motion,
        });
      };

      if (state.motionPair) {
        // Reduced-motion pairs are scoped to the default theme at
        // desktop/fine: motion suppression is theme-independent, and the
        // no-preference control is the pair's baseline.
        for (const motion of ["reduce", "none"] as const) {
          push(
            `${state.id}~rm-${motion}`,
            "dark",
            "fine",
            "desktop",
            false,
            motion,
          );
        }
        continue;
      }

      for (const theme of themes) {
        for (const pointer of pointers) {
          for (const viewportName of viewportNames) {
            push(state.id, theme, pointer, viewportName, false, null);
          }
        }
      }

      if (state.grayscale) {
        for (const theme of CAPTURE_THEMES) {
          push(`${state.id}~gray`, theme, "fine", "desktop", true, null);
        }
      }
    }
  }

  cells.sort((left, right) => left.key.localeCompare(right.key));
  const seen = new Set<string>();
  for (const cell of cells) {
    if (seen.has(cell.key)) {
      throw new Error(`Duplicate capture cell key in roster: ${cell.key}`);
    }
    seen.add(cell.key);
  }
  return cells;
}

export function expandCaptureCellKeys(
  scenarios: readonly CaptureScenarioDescriptor[] = buildCaptureScenarios(),
): string[] {
  return expandCaptureCells(scenarios).map((cell) => cell.key);
}

export function captureRootDir(): string {
  const override = process.env[CAPTURE_ROOT_ENV];
  if (override) {
    return path.resolve(override);
  }
  // Lives under tests/screenshots/, which the repo root .gitignore ignores at
  // any depth — capture output is evidence, never a committed baseline.
  return path.join(MODULE_DIR, "..", "screenshots", "style-captures");
}

/** Run labels become directory names — reject anything path-unsafe. */
export function assertValidRunId(runId: string): void {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(runId)) {
    throw new Error(
      `Invalid capture run id "${runId}": labels must be alphanumeric with ` +
        `dots, dashes, or underscores (e.g. "abc1234-before").`,
    );
  }
}

export function stagingDirFor(rootDir: string, runId: string): string {
  return path.join(rootDir, "staging", runId);
}

export function runDirFor(rootDir: string, runId: string): string {
  return path.join(rootDir, "runs", runId);
}

let cachedGitSha: string | null = null;

export function resolveGitSha(): string {
  if (cachedGitSha === null) {
    cachedGitSha = execSync("git rev-parse HEAD", {
      cwd: MODULE_DIR,
      encoding: "utf8",
    }).trim();
  }
  return cachedGitSha;
}

export function sha256Hex(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}

export interface CaptureCellFragment {
  readonly runId: string;
  readonly cellKey: string;
  readonly scenario: string;
  readonly state: string;
  readonly stateVariant: string;
  readonly theme: CaptureTheme;
  readonly pointer: CapturePointer;
  readonly viewport: CaptureViewportName;
  readonly planSection: string;
  readonly gitSha: string;
  readonly attempt: number;
  readonly pngSha256: string;
}

const FRAGMENT_FILE = "fragment.json";
const PNG_FILE = "capture.png";
const ATTESTATION_FILE = "attested.json";

/**
 * Stage one captured cell for the active run: writes the PNG and its
 * immutable manifest fragment into this attempt's staging directory and
 * attaches the coordinates for the reporter's runner-final attestation.
 * Never writes a success marker itself.
 */
export function stageCaptureCell(
  testInfo: TestInfo,
  cell: CaptureCell,
  png: Buffer,
): CaptureCellFragment {
  const runId = process.env[CAPTURE_RUN_ENV];
  if (!runId) {
    throw new Error(
      `stageCaptureCell requires an active capture run (${CAPTURE_RUN_ENV})`,
    );
  }
  assertValidRunId(runId);
  const attempt = testInfo.retry;
  const attemptDir = path.join(
    stagingDirFor(captureRootDir(), runId),
    cell.key,
    `attempt-${attempt}`,
  );
  // A same-label attempt index can only recur after an aborted earlier
  // process; its partial leavings are superseded by this attempt.
  fs.rmSync(attemptDir, { recursive: true, force: true });
  fs.mkdirSync(attemptDir, { recursive: true });

  const fragment: CaptureCellFragment = {
    runId,
    cellKey: cell.key,
    scenario: cell.scenario,
    state: cell.state,
    stateVariant: cell.stateVariant,
    theme: cell.theme,
    pointer: cell.pointer,
    viewport: cell.viewport.name,
    planSection: cell.planSection,
    gitSha: resolveGitSha(),
    attempt,
    pngSha256: sha256Hex(png),
  };

  fs.writeFileSync(path.join(attemptDir, PNG_FILE), png);
  fs.writeFileSync(
    path.join(attemptDir, FRAGMENT_FILE),
    `${JSON.stringify(fragment, null, 2)}\n`,
  );

  void testInfo.attach(CAPTURE_CELL_ATTACHMENT, {
    body: JSON.stringify({ runId, cellKey: cell.key, attempt, attemptDir }),
    contentType: "application/json",
  });

  return fragment;
}

interface CaptureCellAttachmentBody {
  runId: string;
  cellKey: string;
  attempt: number;
  attemptDir: string;
}

/**
 * Runner-final success attestation. Registered as a Playwright reporter (via
 * `tests/support/captureRunReporter.ts`); `onTestEnd` fires after `afterEach`
 * hooks and test-scoped fixture teardown have settled, so a
 * body-passes/teardown-fails attempt never receives an attestation.
 */
export class CaptureRunReporter implements Reporter {
  private runId: string | undefined;

  onBegin(_config: FullConfig): void {
    this.runId = process.env[CAPTURE_RUN_ENV];
  }

  onTestEnd(_test: TestCase, result: TestResult): void {
    if (!this.runId) {
      return;
    }
    for (const attachment of result.attachments) {
      if (attachment.name !== CAPTURE_CELL_ATTACHMENT || !attachment.body) {
        continue;
      }
      let body: CaptureCellAttachmentBody;
      try {
        body = JSON.parse(attachment.body.toString("utf8"));
      } catch {
        continue;
      }
      if (body.runId !== this.runId) {
        continue;
      }
      if (result.status !== "passed") {
        continue;
      }
      if (!fs.existsSync(path.join(body.attemptDir, FRAGMENT_FILE))) {
        continue;
      }
      fs.writeFileSync(
        path.join(body.attemptDir, ATTESTATION_FILE),
        `${JSON.stringify(
          {
            runId: body.runId,
            cellKey: body.cellKey,
            attempt: body.attempt,
            status: "passed",
          },
          null,
          2,
        )}\n`,
      );
    }
  }

  printsToStdio(): boolean {
    return false;
  }
}

export interface FinalizeOptions {
  readonly runId: string;
  readonly rootDir: string;
  readonly expectedKeys: readonly string[];
}

export interface FinalizedRunManifest {
  readonly runId: string;
  readonly gitSha: string;
  readonly cellCount: number;
  readonly cells: Record<
    string,
    {
      readonly file: string;
      readonly pngSha256: string;
      readonly scenario: string;
      readonly stateVariant: string;
      readonly theme: CaptureTheme;
      readonly pointer: CapturePointer;
      readonly viewport: CaptureViewportName;
      readonly planSection: string;
      readonly attempt: number;
    }
  >;
}

function readJson<T>(filePath: string): T {
  return JSON.parse(fs.readFileSync(filePath, "utf8")) as T;
}

/**
 * Assemble staged cells into the immutable labeled run directory.
 * Publishes only on exact expected-key-set equality; one atomic rename.
 */
export function finalizeCaptureRun(
  options: FinalizeOptions,
): FinalizedRunManifest {
  const { runId, rootDir, expectedKeys } = options;
  assertValidRunId(runId);
  const stagingDir = stagingDirFor(rootDir, runId);
  const runDir = runDirFor(rootDir, runId);
  const runsDir = path.dirname(runDir);

  if (fs.existsSync(runDir)) {
    throw new Error(
      `Capture run "${runId}" is already finalized at ${runDir}; ` +
        `finalized runs are immutable — pick a new label.`,
    );
  }

  const expected = new Set(expectedKeys);
  const stagedKeys = fs.existsSync(stagingDir)
    ? fs
        .readdirSync(stagingDir, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name)
    : [];

  const foreign = stagedKeys.filter((key) => !expected.has(key)).sort();
  if (foreign.length > 0) {
    throw new Error(
      `Refusing to finalize capture run "${runId}": staging contains ` +
        `${foreign.length} cell key(s) outside the expected matrix ` +
        `(foreign work): ${foreign.join(", ")}`,
    );
  }

  const selected = new Map<
    string,
    { attemptDir: string; fragment: CaptureCellFragment }
  >();
  for (const key of stagedKeys) {
    const cellDir = path.join(stagingDir, key);
    const attempts = fs
      .readdirSync(cellDir, { withFileTypes: true })
      .filter(
        (entry) => entry.isDirectory() && /^attempt-\d+$/.test(entry.name),
      )
      .map((entry) => ({
        dir: path.join(cellDir, entry.name),
        index: Number(entry.name.slice("attempt-".length)),
      }))
      // Duplicate successful fragments (CI retries) resolve deterministically
      // to the highest attempt index.
      .sort((left, right) => right.index - left.index);

    for (const attempt of attempts) {
      const fragmentPath = path.join(attempt.dir, FRAGMENT_FILE);
      const attestationPath = path.join(attempt.dir, ATTESTATION_FILE);
      const pngPath = path.join(attempt.dir, PNG_FILE);
      if (
        !fs.existsSync(fragmentPath) ||
        !fs.existsSync(attestationPath) ||
        !fs.existsSync(pngPath)
      ) {
        continue;
      }
      const fragment = readJson<CaptureCellFragment>(fragmentPath);
      if (fragment.cellKey !== key || fragment.runId !== runId) {
        throw new Error(
          `Refusing to finalize capture run "${runId}": fragment at ` +
            `${fragmentPath} does not match its staging location.`,
        );
      }
      const png = fs.readFileSync(pngPath);
      if (sha256Hex(png) !== fragment.pngSha256) {
        throw new Error(
          `Refusing to finalize capture run "${runId}": PNG for cell ` +
            `"${key}" (attempt ${fragment.attempt}) does not match its ` +
            `recorded hash — staging is corrupt.`,
        );
      }
      selected.set(key, { attemptDir: attempt.dir, fragment });
      break;
    }
  }

  const missing = [...expected].filter((key) => !selected.has(key)).sort();
  if (missing.length > 0) {
    if (selected.size === 0 && stagedKeys.length > 0) {
      throw new Error(
        `Refusing to finalize capture run "${runId}": ${stagedKeys.length} ` +
          `cell(s) staged fragments but NONE carry a success attestation. ` +
          `This almost always means the CaptureRunReporter was not ` +
          `registered — a CLI --reporter flag REPLACES the config reporter ` +
          `list. Re-run without --reporter.`,
      );
    }
    throw new Error(
      `Refusing to finalize capture run "${runId}": ` +
        `${missing.length} of ${expected.size} expected cell(s) have no ` +
        `successful attested capture: ${missing.join(", ")}`,
    );
  }

  const shas = new Set(
    [...selected.values()].map(({ fragment }) => fragment.gitSha),
  );
  if (shas.size > 1) {
    throw new Error(
      `Refusing to finalize capture run "${runId}": staging mixes captures ` +
        `from different git SHAs (${[...shas].sort().join(", ")}) — stale ` +
        `staging from an earlier run at different code. Clear ` +
        `${stagingDir} and re-run.`,
    );
  }
  const gitSha = [...shas][0] ?? "unknown";

  fs.mkdirSync(runsDir, { recursive: true });
  // Clean any orphaned temp dir left by an interrupted finalization of this
  // label — the label itself was never occupied, so recovery is a re-run.
  for (const entry of fs.readdirSync(runsDir)) {
    if (entry.startsWith(`.publish-${runId}-`)) {
      fs.rmSync(path.join(runsDir, entry), { recursive: true, force: true });
    }
  }
  const tempDir = path.join(
    runsDir,
    `.publish-${runId}-${process.pid}-${Math.random().toString(36).slice(2, 10)}`,
  );
  fs.mkdirSync(tempDir, { recursive: true });

  const cells: Record<string, FinalizedRunManifest["cells"][string]> = {};

  for (const [key, { attemptDir, fragment }] of [...selected.entries()].sort(
    ([left], [right]) => left.localeCompare(right),
  )) {
    const fileName = `${key}.png`;
    fs.copyFileSync(
      path.join(attemptDir, PNG_FILE),
      path.join(tempDir, fileName),
    );
    cells[key] = {
      file: fileName,
      pngSha256: fragment.pngSha256,
      scenario: fragment.scenario,
      stateVariant: fragment.stateVariant,
      theme: fragment.theme,
      pointer: fragment.pointer,
      viewport: fragment.viewport,
      planSection: fragment.planSection,
      attempt: fragment.attempt,
    };
  }

  const manifest: FinalizedRunManifest = {
    runId,
    gitSha,
    cellCount: selected.size,
    cells,
  };

  fs.writeFileSync(
    path.join(tempDir, "run-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );

  // Single atomic publish; refuse if the label got occupied concurrently.
  try {
    fs.renameSync(tempDir, runDir);
  } catch (error) {
    fs.rmSync(tempDir, { recursive: true, force: true });
    throw error;
  }

  // Staging is scratch once the label is occupied.
  fs.rmSync(stagingDir, { recursive: true, force: true });

  return manifest;
}

/**
 * Playwright globalTeardown — the only publisher. Run-level, never spec- or
 * worker-scoped, so parallel workers cannot each publish. Exits without
 * touching staging when no capture run id is active.
 */
export default async function captureRunGlobalTeardown(): Promise<void> {
  const runId = process.env[CAPTURE_RUN_ENV];
  if (!runId) {
    return;
  }
  const manifest = finalizeCaptureRun({
    runId,
    rootDir: captureRootDir(),
    expectedKeys: expandCaptureCellKeys(),
  });
  // eslint-disable-next-line no-console
  console.log(
    `[style-captures] finalized run "${manifest.runId}" ` +
      `(${manifest.cellCount} cells, git ${manifest.gitSha.slice(0, 12)}) → ` +
      runDirFor(captureRootDir(), runId),
  );
}
