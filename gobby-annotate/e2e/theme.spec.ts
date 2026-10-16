import { expect, test, type Locator } from "@playwright/test";
import { cp, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

type Measure = {
  name: string;
  disabled: boolean;
  text: number;
  separation: number;
};

// Resolves every colour through a canvas so oklch() and color-mix() compare as
// sRGB, then applies WCAG 2.2 relative luminance.
async function measure(panel: Locator): Promise<Measure[]> {
  return panel.evaluate((root) => {
    const ctx = document.createElement("canvas").getContext("2d")!;
    const rgba = (color: string) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, 1, 1);
      return [...ctx.getImageData(0, 0, 1, 1).data];
    };
    const luminance = ([r, g, b]: number[]) =>
      [r!, g!, b!]
        .map((v) => v / 255)
        .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
        .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i]!, 0);
    const ratio = (a: number[], b: number[]) => {
      const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
      return (hi! + 0.05) / (lo! + 0.05);
    };
    const ground = rgba(getComputedStyle(root).backgroundColor);
    return [...root.querySelectorAll("button")].map((button) => {
      const style = getComputedStyle(button);
      const fill = rgba(style.backgroundColor);
      const face = fill[3] === 0 ? ground : fill;
      const edge = rgba(style.borderTopColor);
      const bordered = parseFloat(style.borderTopWidth) > 0 && edge[3]! > 0;
      return {
        name: button.getAttribute("aria-label") ?? button.textContent ?? "",
        disabled: button.disabled,
        text: ratio(rgba(style.color), face),
        separation: Math.max(
          ratio(face, ground),
          bordered ? ratio(edge, ground) : 1,
        ),
      };
    });
  });
}

test("both themes give every Annotate button AA text and a 3:1 edge or fill against the panel", async ({
  playwright,
}, testInfo) => {
  test.setTimeout(90_000);
  const evidence = process.env.ANNOTATE_EVIDENCE_DIR;
  const temporary = await mkdtemp(join(tmpdir(), "annotate-theme-"));
  const extension = join(temporary, "extension");
  // ANNOTATE_EXTENSION_DIR points at an older build to capture "before" evidence.
  const build =
    process.env.ANNOTATE_EXTENSION_DIR ?? "packages/extension/dist/chrome";
  await cp(resolve(build), extension, {
    recursive: true,
  });
  const manifest = JSON.parse(
    await readFile(join(extension, "manifest.json"), "utf8"),
  );
  manifest.host_permissions = ["<all_urls>"];
  await writeFile(join(extension, "manifest.json"), JSON.stringify(manifest));
  const context = await playwright.chromium.launchPersistentContext(
    join(temporary, "profile"),
    {
      channel: "chromium",
      headless: true,
      viewport: { width: 900, height: 900 },
      args: [
        `--disable-extensions-except=${extension}`,
        `--load-extension=${extension}`,
      ],
    },
  );
  try {
    const worker =
      context.serviceWorkers()[0] ??
      (await context.waitForEvent("serviceworker"));
    const page = await context.newPage();
    await context.route("https://annotate.test/**", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: '<html><body style="margin:0;background:#eee"><h1 style="font:600 28px system-ui;margin:420px 40px">Checkout</h1></body></html>',
      }),
    );
    await page.goto("https://annotate.test/");
    await worker.evaluate(async () => {
      const [tab] = await chrome.tabs.query({ url: "https://annotate.test/*" });
      await chrome.scripting.executeScript({
        target: { tabId: tab!.id! },
        files: ["content.js"],
      });
    });
    await page.getByRole("button", { name: "Batch and export menu" }).click();
    const panel = page.getByRole("region", { name: "Batch and export" });
    await page.getByRole("button", { name: "New batch" }).click();
    await expect(panel).toBeVisible();
    const shell = page.locator(".annotate-shell");
    const results: Record<string, Measure[]> = {};
    for (const theme of ["dark", "light"]) {
      await page.mouse.move(0, 0);
      await shell.screenshot({
        path: evidence
          ? join(evidence, `annotate-panel-${theme}.png`)
          : testInfo.outputPath(`annotate-panel-${theme}.png`),
      });
      results[theme] = await measure(panel);
      if (theme === "dark")
        await page.getByRole("button", { name: "Switch theme" }).click();
    }
    await testInfo.attach("contrast", {
      body: JSON.stringify(results, null, 1),
      contentType: "application/json",
    });
    for (const [theme, buttons] of Object.entries(results)) {
      expect(buttons.length, theme).toBeGreaterThan(4);
      for (const b of buttons.filter((b) => !b.disabled)) {
        expect(b.text, `${theme} ${b.name} text`).toBeGreaterThanOrEqual(4.5);
        expect(
          b.separation,
          `${theme} ${b.name} edge or fill`,
        ).toBeGreaterThanOrEqual(3);
      }
    }
  } finally {
    await context.close();
  }
});
