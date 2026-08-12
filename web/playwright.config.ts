import { defineConfig } from "@playwright/test";

const PLAYWRIGHT_BASE_URL =
  process.env.PLAYWRIGHT_BASE_URL || "http://localhost:60889";
const SHOULD_START_WEBSERVER = !process.env.PLAYWRIGHT_BASE_URL;

// Style-surface capture cells are tagged @style-capture and run only in the
// dedicated project below (opt-in via GOBBY_CAPTURE_RUN_ID — see the header
// of tests/style-surfaces.spec.ts). The default project excludes them so an
// ordinary run never produces capture work.
const STYLE_CAPTURE_TAG = /@style-capture/;

// Shared capture-project options. Full Chromium (not the headless shell):
// the knowledge-graph capture needs real WebGL, and capture fidelity beats
// startup speed. GOBBY_CAPTURE_BROWSER overrides with an explicit
// Chromium-family executable for machines where the Playwright browser
// bundle is not installable; pairing before/after runs on one machine keeps
// rendering consistent either way.
const STYLE_CAPTURE_USE = {
  browserName: "chromium" as const,
  ...(process.env.GOBBY_CAPTURE_BROWSER ? {} : { channel: "chromium" }),
  // Stable pixel geometry for pairable captures.
  deviceScaleFactor: 1,
  launchOptions: {
    ...(process.env.GOBBY_CAPTURE_BROWSER
      ? { executablePath: process.env.GOBBY_CAPTURE_BROWSER }
      : {}),
    // The voice-recording capture cells exercise the live mic-recording
    // animation against a fake capture device. The remaining flags force
    // fully deterministic rasterization: GPU raster/compositing off (tile AA
    // flicker, ±1-channel blend rounding on layer edges), partial raster off
    // (damage-region reuse leaves ±1 seams on anti-aliased arcs at
    // fractional offsets), Skia runtime opts off and a pinned color profile
    // (machine-dependent LSB shifts). Unlike a full --disable-gpu this set
    // keeps WebGL alive for the knowledge graph.
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      "--disable-gpu-rasterization",
      "--disable-gpu-compositing",
      "--disable-partial-raster",
      "--disable-skia-runtime-opts",
      "--force-color-profile=srgb",
    ],
  },
};

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  // NOTE: passing --reporter on the CLI REPLACES this list and silently
  // drops the capture attestation reporter — never override it on capture
  // runs (the finalizer aborts loudly if that happens).
  reporter: [
    ["html", { open: "never" }],
    ["./tests/support/captureRunReporter.ts"],
  ],
  // Run-level capture finalizer: a no-op unless GOBBY_CAPTURE_RUN_ID is set.
  globalTeardown: "./tests/support/captureRunFinalizer.ts",
  use: {
    baseURL: PLAYWRIGHT_BASE_URL,
    trace: "on-first-retry",
  },
  ...(SHOULD_START_WEBSERVER
    ? {
        webServer: {
          command: "npm run dev",
          url: PLAYWRIGHT_BASE_URL,
          reuseExistingServer: !process.env.CI,
        },
      }
    : {}),
  projects: [
    {
      name: "chromium",
      // GOBBY_CAPTURE_BROWSER doubles as the plain-project escape hatch on
      // machines where the Playwright browser bundle is not installable —
      // the same override the capture projects document above.
      use: {
        browserName: "chromium",
        ...(process.env.GOBBY_CAPTURE_BROWSER
          ? {
              launchOptions: {
                executablePath: process.env.GOBBY_CAPTURE_BROWSER,
              },
            }
          : {}),
      },
      grepInvert: STYLE_CAPTURE_TAG,
    },
    {
      name: "style-capture",
      // Fine-pointer capture cells. The pointer axis must be split at the
      // PROJECT level: the runner derives launch-time blink settings from the
      // project's `use`, and a per-context hasTouch cannot override them.
      grep: STYLE_CAPTURE_TAG,
      grepInvert: /--coarse--/,
      use: STYLE_CAPTURE_USE,
    },
    {
      name: "style-capture-coarse",
      // Coarse-pointer capture cells (cell keys carry `--coarse--`).
      grep: /--coarse--/,
      use: { ...STYLE_CAPTURE_USE, hasTouch: true },
    },
  ],
});
