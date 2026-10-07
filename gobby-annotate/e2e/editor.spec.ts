import { expect, test } from "@playwright/test";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { decodeBundle } from "../packages/core/src";
import { build } from "esbuild";

declare global {
  interface Window {
    editorBarrier: typeof import("../packages/extension/src/focus");
    releaseEditor: () => void;
  }
}

test("second mobile annotation survives a page focus trap and restores page gestures", async ({
  playwright,
}, testInfo) => {
  test.setTimeout(120_000);
  const temporary = await mkdtemp(join(tmpdir(), "annotate-editor-"));
  const extension = join(temporary, "extension");
  await cp(resolve("packages/extension/dist/chrome"), extension, {
    recursive: true,
  });
  const manifest = JSON.parse(
    await readFile(join(extension, "manifest.json"), "utf8"),
  );
  // captureVisibleTab needs activeTab or <all_urls>, rather than a single host
  // grant; the browser action cannot be pressed in this disposable automation.
  manifest.host_permissions = ["<all_urls>"];
  await writeFile(join(extension, "manifest.json"), JSON.stringify(manifest));
  const context = await playwright.chromium.launchPersistentContext(
    join(temporary, "profile"),
    {
      channel: "chromium",
      headless: true,
      viewport: { width: 440, height: 956 },
      hasTouch: true,
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
    const cdp = await context.newCDPSession(page);
    await cdp.send("Browser.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: join(temporary, "downloads"),
    });
    // Controlled Reddit-shaped fixture: app banner, long feed and a focus trap
    // that appears after the first note. No writes to Reddit or live user state.
    await context.route("https://www.reddit.com/**", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: `<meta name="viewport" content="width=device-width,initial-scale=1"><body style="margin:0;min-height:4000px"><header style="position:sticky;top:0">Reddit — Open in the Reddit app <button id="open">Open</button></header><p>Feed content</p><script>document.addEventListener('touchmove', () => document.body.dataset.gestures = String(Number(document.body.dataset.gestures || 0) + 1));</script></body>`,
      }),
    );
    await page.goto("https://www.reddit.com/");
    await page.bringToFront();
    await page.evaluate(() => window.scrollTo(0, 240));
    await worker.evaluate(async () => {
      const tabs = await chrome.tabs.query({ url: "https://www.reddit.com/*" });
      await chrome.scripting.executeScript({
        target: { tabId: tabs[0].id! },
        files: ["content.js"],
      });
    });
    const swipe = async (x: number, y: number) => {
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchStart",
        touchPoints: [{ x, y }],
      });
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchMove",
        touchPoints: [{ x, y: y - 120 }],
      });
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchEnd",
        touchPoints: [],
      });
    };
    for (const comment of ["First mobile note", "Second mobile note"]) {
      await page
        .getByRole("button", { name: "Select rectangle", exact: true })
        .tap();
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchStart",
        touchPoints: [{ x: 40, y: 650 }],
      });
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchMove",
        touchPoints: [{ x: 300, y: 710 }],
      });
      await cdp.send("Input.dispatchTouchEvent", {
        type: "touchEnd",
        touchPoints: [],
      });
      const textarea = page.getByLabel("What should change?");
      await expect(textarea).toBeVisible({ timeout: 30_000 });
      // The shared label expands the coarse-pointer hit area over the field.
      // Tap the visible geometry so the browser performs its real label default,
      // rather than rejecting the label as a locator interception.
      const target = (await textarea.boundingBox())!;
      await page.touchscreen.tap(
        target.x + target.width / 2,
        target.y + target.height / 2,
      );
      await expect(textarea).toBeFocused();
      await page.keyboard.type(comment);
      await expect(textarea).toHaveValue(comment);
      await expect(page.getByRole("status")).toHaveText("Saved locally");
      const locked = await page.evaluate(() => ({
        y: scrollY,
        gestures: document.body.dataset.gestures,
      }));
      const box = (await textarea.boundingBox())!;
      await swipe(box.x + 20, box.y + 60);
      await swipe(420, 800);
      expect(
        await page.evaluate(() => ({
          y: scrollY,
          gestures: document.body.dataset.gestures,
        })),
      ).toEqual(locked);
      await expect(textarea).toBeFocused();
      await page.screenshot({
        path: testInfo.outputPath(
          `${comment.includes("Second") ? "second" : "first"}-editor.png`,
        ),
      });
      if (comment.startsWith("First")) await page.keyboard.press("Escape");
      else await page.getByRole("button", { name: "Done", exact: true }).tap();
      await expect(textarea).toHaveCount(0);
      expect(
        await page.evaluate(() => ({ inert: document.body.inert, y: scrollY })),
      ).toEqual({ inert: false, y: 240 });
      if (comment.startsWith("First")) {
        await page.evaluate(() =>
          window.addEventListener(
            "focusin",
            (event) => {
              if (event.target !== document.getElementById("open"))
                document.getElementById("open")!.focus();
            },
            true,
          ),
        );
      }
    }
    await page.getByRole("button", { name: "Batch and export menu" }).tap();
    await page.getByRole("button", { name: "Export ZIP", exact: true }).tap();
    await expect
      .poll(() =>
        worker.evaluate(
          async () =>
            (await chrome.downloads.search({ state: "complete" })).length,
        ),
      )
      .toBe(1);
    const filename = await worker.evaluate(
      async () =>
        (await chrome.downloads.search({ state: "complete" }))[0].filename,
    );
    const bytes = await readFile(filename);
    expect(
      decodeBundle(bytes).manifest.annotations.map((note) => note.comment),
    ).toEqual(["First mobile note", "Second mobile note"]);
    await writeFile(testInfo.outputPath("second-annotation.zip"), bytes);
    await page
      .getByRole("button", { name: "Annotations (2)", exact: true })
      .tap();
    await page
      .getByRole("button", { name: "Second mobile note", exact: true })
      .tap();
    await expect(page.getByLabel("What should change?")).toHaveValue(
      "Second mobile note",
    );
    // Browser-action deactivation while the editor is still mounted must run
    // the same cleanup as Done, without losing the persisted second note.
    await page
      .locator("#gobby-annotate-root")
      .evaluate((node) => node.dispatchEvent(new Event("annotate-deactivate")));
    await expect(page.locator("#gobby-annotate-root")).toHaveCount(0);
    expect(
      await page.evaluate(() => ({ inert: document.body.inert, y: scrollY })),
    ).toEqual({ inert: false, y: 240 });
    await swipe(220, 800);
    await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(240);
    await page.locator("#open").tap();
    await expect(page.locator("#open")).toBeFocused();
  } finally {
    await context.close();
    await rm(temporary, { recursive: true, force: true });
  }
});

test("WebKit holds native textarea focus on consecutive editors and restores the page", async ({
  playwright,
}) => {
  const compiled = await build({
    entryPoints: ["packages/extension/src/focus.ts"],
    bundle: true,
    write: false,
    format: "iife",
    globalName: "editorBarrier",
  });
  const browser = await playwright.webkit.launch();
  const context = await browser.newContext({
    viewport: { width: 440, height: 956 },
    hasTouch: true,
  });
  try {
    const page = await context.newPage();
    await page.setContent(
      `<meta name="viewport" content="width=device-width,initial-scale=1"><body style="margin:0;min-height:4000px"><button id="open" style="position:fixed;top:0">Open in the Reddit app</button></body>`,
    );
    await page.evaluate(() => {
      window.scrollTo(0, 240);
      document.getElementById("open")!.addEventListener("click", (event) => {
        const button = event.currentTarget as HTMLButtonElement;
        button.dataset.taps = String(Number(button.dataset.taps ?? 0) + 1);
      });
      window.addEventListener(
        "focusin",
        (event) => {
          if (event.target !== document.getElementById("open"))
            document.getElementById("open")!.focus();
        },
        true,
      );
      const host = document.createElement("div");
      host.id = "editor";
      host.style.cssText =
        "position:fixed;inset:0;pointer-events:auto;z-index:2147483647";
      host.attachShadow({ mode: "open" }).innerHTML =
        `<div style="position:fixed;top:100px;left:8px;width:360px;pointer-events:auto"><textarea aria-label="Comment" style="width:340px;height:100px;font-size:16px;overscroll-behavior:contain"></textarea></div>`;
      document.documentElement.append(host);
    });
    await page.addScriptTag({ content: compiled.outputFiles[0].text });
    await page.getByLabel("Comment").tap();
    await expect(page.locator("#open")).toBeFocused();
    let pageTaps = 0;
    for (const comment of ["First WebKit note", "Second WebKit note"]) {
      await page.evaluate(() => {
        window.releaseEditor = window.editorBarrier.isolateEditor(
          document.getElementById("editor")!,
        );
      });
      const textarea = page.getByLabel("Comment");
      await textarea.tap();
      await expect(textarea).toBeFocused();
      await page.keyboard.type(comment);
      await expect(textarea).toHaveValue(comment);
      const locked = await page.evaluate(() => scrollY);
      await page.mouse.move(420, 800);
      await page.mouse.wheel(0, 500);
      await expect.poll(() => page.evaluate(() => scrollY)).toBe(locked);
      await expect(textarea).toBeFocused();
      await page.evaluate(() => {
        window.releaseEditor();
        document.getElementById("editor")!.style.pointerEvents = "none";
      });
      expect(
        await page.evaluate(() => ({ inert: document.body.inert, y: scrollY })),
      ).toEqual({ inert: false, y: 240 });
      await page.locator("#open").tap();
      await expect(page.locator("#open")).toHaveAttribute(
        "data-taps",
        String(++pageTaps),
      );
      // WebKit buttons do not take focus on a native tap; prove that the page
      // can receive focus again independently of that platform convention.
      await page.locator("#open").evaluate((button) => button.focus());
      await expect(page.locator("#open")).toBeFocused();
      await page.evaluate(() => {
        const field = document
          .getElementById("editor")!
          .shadowRoot!.querySelector("textarea")!;
        const next = field.cloneNode() as HTMLTextAreaElement;
        next.value = "";
        field.replaceWith(next);
      });
    }
    await page.evaluate(() => document.getElementById("editor")!.remove());
    await page.mouse.move(220, 800);
    await page.mouse.wheel(0, 500);
    await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(240);
  } finally {
    await context.close();
    await browser.close();
  }
});
