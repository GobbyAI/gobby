import { expect, test } from "@playwright/test";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { decodeBundle } from "../packages/core/src";

test("native capture, persistent editing, responsive controls and idempotent activation", async ({
  playwright,
}, testInfo) => {
  const temporary = await mkdtemp(join(tmpdir(), "annotate-chrome-"));
  const extension = join(temporary, "extension");
  await cp(resolve("packages/extension/dist/chrome"), extension, {
    recursive: true,
  });
  const manifest = JSON.parse(
    await readFile(join(extension, "manifest.json"), "utf8"),
  );
  // Automation cannot press the browser action. Grant only this disposable build
  // host access so it can exercise the same injection and native screenshot path.
  manifest.host_permissions = ["<all_urls>"];
  await writeFile(join(extension, "manifest.json"), JSON.stringify(manifest));
  const context = await playwright.chromium.launchPersistentContext(
    join(temporary, "profile"),
    {
      channel: "chromium",
      headless: true,
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
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await context.route("https://annotate.test/**", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: '<html><body style="margin:0;background:#eee"><button id="target" style="position:absolute;left:80px;top:180px;width:120px;height:60px" onclick="this.textContent=\'activated\'">Checkout</button></body></html>',
      }),
    );
    await page.goto("https://annotate.test/");
    await page.bringToFront();
    const activate = () =>
      worker.evaluate(async () => {
        const tabs = await chrome.tabs.query({
          url: "https://annotate.test/*",
        });
        const results = await chrome.scripting.executeScript({
          target: { tabId: tabs[0].id! },
          files: ["content.js"],
        });
        return results;
      });
    await activate();
    await activate();
    await expect(page.locator("#gobby-annotate-root")).toHaveCount(1);
    await expect(
      page.getByRole("toolbar", { name: "Gobby Annotate" }),
    ).toBeVisible();
    for (const [width, height] of [
      [320, 700],
      [440, 956],
      [932, 430],
      [1440, 900],
    ]) {
      await page.setViewportSize({ width, height });
      await expect
        .poll(async () => {
          const box = await page.getByRole("toolbar").boundingBox();
          return (
            !!box &&
            box.x >= 0 &&
            box.y >= 0 &&
            box.x + box.width <= width &&
            box.y + box.height <= height
          );
        })
        .toBe(true);
      await page.screenshot({
        path: testInfo.outputPath(`toolbar-${width}x${height}.png`),
      });
    }
    await expect
      .poll(() =>
        page.evaluate(() =>
          [...document.fonts]
            .filter((font) => font.status === "loaded")
            .map((font) => font.family.replaceAll('"', "")),
        ),
      )
      .toContain("Geist Variable");
    const toolbar = (await page.getByRole("toolbar").boundingBox())!;
    await page
      .getByRole("button", { name: "Select element", exact: true })
      .click();
    await page.mouse.click(140, 210);
    await expect(
      page.getByRole("region", { name: "Edit annotation" }),
    ).toBeVisible();
    // Fail one actual draft transaction, then restore the browser API for retry.
    const failNextDraft = () =>
      worker.evaluate(() => {
        const put = IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.put = function (
          ...args: Parameters<IDBObjectStore["put"]>
        ) {
          if (this.name === "batches") {
            IDBObjectStore.prototype.put = put;
            throw new DOMException(
              "Simulated storage quota failure",
              "QuotaExceededError",
            );
          }
          return put.apply(this, args);
        };
      });
    await failNextDraft();
    await page.getByLabel("What should change?").fill("Make checkout clearer");
    await expect(page.getByRole("alert")).toContainText(
      "Simulated storage quota failure",
    );
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page
      .getByRole("button", { name: "Select rectangle", exact: true })
      .click();
    await page.getByRole("button", { name: "Collapse toolbar" }).click();
    await page.getByRole("button", { name: "Batch and export menu" }).click();
    await page.evaluate(() =>
      document
        .getElementById("gobby-annotate-root")!
        .dispatchEvent(new Event("annotate-deactivate")),
    );
    await expect(page.getByLabel("What should change?")).toHaveValue(
      "Make checkout clearer",
    );
    await expect(
      page.getByRole("button", { name: "Export ZIP", exact: true }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Retry save", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("Saved locally");
    const other = await context.newPage();
    await other.goto("https://annotate.test/second");
    await worker.evaluate(async () => {
      const tabs = await chrome.tabs.query({
        url: "https://annotate.test/second",
      });
      await chrome.scripting.executeScript({
        target: { tabId: tabs[0].id! },
        files: ["content.js"],
      });
    });
    await other.getByRole("button", { name: "Annotations (1)" }).click();
    await other
      .getByRole("button", { name: "Make checkout clearer", exact: true })
      .click();
    await other
      .getByLabel("What should change?")
      .fill("Changed in another tab");
    await expect(other.getByRole("status")).toHaveText("Saved locally");
    await other.close();
    await page.bringToFront();
    await page
      .getByLabel("What should change?")
      .fill("Conflicting local change");
    await expect(page.getByRole("alert")).toContainText(
      "changed in another tab",
    );
    await expect(page.getByLabel("What should change?")).toHaveValue(
      "Conflicting local change",
    );
    await page
      .getByRole("button", { name: "Discard unsaved changes", exact: true })
      .click();
    await page.getByRole("button", { name: "Annotations (1)" }).click();
    await page
      .getByRole("button", { name: "Changed in another tab", exact: true })
      .click();
    await page.getByLabel("What should change?").fill("Make checkout clearer");
    await expect(page.getByRole("status")).toHaveText("Saved locally");
    await page.getByText("Review screenshot", { exact: true }).click();
    const screenshot = page.getByRole("img", {
      name: "Original visible page at selection time",
    });
    await expect(screenshot).toHaveAttribute("src", /^data:image\/png;base64,/);
    expect(
      await screenshot.evaluate((node: HTMLImageElement) => node.naturalWidth),
    ).toBe(1440);
    const pixel = await screenshot.evaluate(
      (node: HTMLImageElement, point) => {
        const canvas = document.createElement("canvas");
        canvas.width = node.naturalWidth;
        canvas.height = node.naturalHeight;
        const context = canvas.getContext("2d")!;
        context.drawImage(node, 0, 0);
        return [...context.getImageData(point.x, point.y, 1, 1).data];
      },
      {
        x: Math.round(toolbar.x + toolbar.width / 2),
        y: Math.round(toolbar.y + toolbar.height / 2),
      },
    );
    expect(pixel).toEqual([238, 238, 238, 255]);
    await page.screenshot({ path: testInfo.outputPath("editor-desktop.png") });
    await expect(page.locator("#target")).toHaveText("Checkout");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.getByRole("button", { name: "Batch and export menu" }).click();
    await failNextDraft();
    await page
      .getByLabel("Batch name", { exact: true })
      .fill("Checkout review");
    await expect(page.getByRole("alert")).toContainText(
      "Simulated storage quota failure",
    );
    await page.getByRole("button", { name: "Export ZIP", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("not saved yet");
    expect(
      await worker.evaluate(
        async () => (await chrome.downloads.search({})).length,
      ),
    ).toBe(0);
    await page.getByRole("button", { name: "Collapse toolbar" }).click();
    await expect(page.getByLabel("Batch name", { exact: true })).toHaveValue(
      "Checkout review",
    );
    await page.getByRole("button", { name: "Retry save", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("Saved locally");
    await page.getByRole("button", { name: "Export ZIP", exact: true }).click();
    await expect
      .poll(async () =>
        worker.evaluate(async () => {
          const downloads = await chrome.downloads.search({});
          return downloads.map((item) => ({
            state: item.state,
            error: item.error,
            filename: item.filename,
          }));
        }),
      )
      .toEqual([expect.objectContaining({ state: "complete" })]);
    const exported = await worker.evaluate(async () => {
      const downloads = await chrome.downloads.search({});
      return downloads.find((item) => item.filename.endsWith(".zip"))!.filename;
    });
    const exportBytes = await readFile(exported);
    const bundle = decodeBundle(exportBytes);
    expect(bundle.manifest.annotations).toHaveLength(1);
    expect(bundle.assets.size).toBe(1);
    await writeFile(testInfo.outputPath("capture.zip"), exportBytes);
    await page.reload();
    await activate();
    await expect(
      page.getByRole("button", { name: "Annotations (1)", exact: true }),
    ).toBeVisible();
    await page.evaluate(() => {
      const frame = document.createElement("iframe");
      frame.id = "preview";
      frame.style.cssText =
        "position:absolute;left:400px;top:100px;width:400px;height:300px;border:0;transform:scale(.5);transform-origin:top left";
      frame.srcdoc =
        '<body style="margin:0;height:1000px"><button id="inside" style="position:absolute;left:20px;top:150px;width:100px;height:40px">Preview action</button>';
      document.body.append(frame);
      const host = document.createElement("div");
      host.id = "open-shadow";
      host.style.cssText = "position:absolute;left:700px;top:100px";
      host.attachShadow({ mode: "open" }).innerHTML =
        '<button id="shadow-action" style="width:120px;height:40px">Shadow action</button>';
      document.body.append(host);
      const restricted = document.createElement("iframe");
      restricted.id = "restricted";
      restricted.setAttribute("sandbox", "");
      restricted.srcdoc = "<button>Inaccessible action</button>";
      restricted.style.cssText =
        "position:absolute;left:900px;top:100px;width:200px;height:100px;border:0";
      document.body.append(restricted);
    });
    await expect(
      page.frameLocator("#preview").locator("#inside"),
    ).toBeVisible();
    await page
      .frameLocator("#preview")
      .locator("body")
      .evaluate(() => window.scrollTo(0, 100));
    const saveSelection = async (x: number, y: number, comment: string) => {
      await page
        .getByRole("button", { name: "Select element", exact: true })
        .click();
      await page.mouse.click(x, y);
      await expect
        .poll(async () => {
          if (await page.getByLabel("What should change?").isVisible())
            return "editor";
          return page.getByRole("alert").allTextContents();
        })
        .toBe("editor");
      await page.getByLabel("What should change?").fill(comment);
      await expect(page.getByRole("status")).toHaveText("Saved locally");
      await page.getByRole("button", { name: "Done", exact: true }).click();
    };
    await saveSelection(430, 135, "Scaled preview target");
    await saveSelection(740, 120, "Open shadow target");
    await saveSelection(960, 130, "Restricted frame host");
    await page
      .getByRole("button", { name: "Select rectangle", exact: true })
      .click();
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchStart",
      touchPoints: [{ x: 300, y: 300 }],
    });
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ x: 380, y: 350 }],
    });
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchEnd",
      touchPoints: [],
    });
    await page.getByLabel("What should change?").fill("Touch region");
    await expect(page.getByRole("status")).toHaveText("Saved locally");
    await page.setViewportSize({ width: 320, height: 280 });
    const editor = await page
      .getByRole("region", { name: "Edit annotation" })
      .boundingBox();
    expect(editor!.x).toBeGreaterThanOrEqual(0);
    expect(editor!.y).toBeGreaterThanOrEqual(0);
    expect(editor!.x + editor!.width).toBeLessThanOrEqual(320);
    expect(editor!.y + editor!.height).toBeLessThanOrEqual(280);
    await page.screenshot({
      path: testInfo.outputPath("editor-small-visible-area.png"),
    });
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.getByRole("button", { name: "Collapse toolbar" }).click();
    const collapsed = page.getByRole("button", {
      name: "Expand Gobby Annotate",
    });
    const beforeDrag = (await collapsed.boundingBox())!;
    expect(beforeDrag.width).toBeGreaterThanOrEqual(44);
    expect(beforeDrag.height).toBeGreaterThanOrEqual(44);
    await page.mouse.move(beforeDrag.x + 20, beforeDrag.y + 20);
    await page.mouse.down();
    await page.mouse.move(beforeDrag.x + 120, beforeDrag.y + 20, { steps: 5 });
    await page.mouse.up();
    await expect(collapsed).toBeVisible();
    expect((await collapsed.boundingBox())!.x).toBeGreaterThan(
      beforeDrag.x + 50,
    );
    await collapsed.click();
    await page.getByRole("button", { name: "Batch and export menu" }).click();
    const panelBackground = () =>
      page
        .getByRole("toolbar")
        .evaluate((node) => getComputedStyle(node).backgroundColor);
    const initialBackground = await panelBackground();
    await page.getByRole("button", { name: "Switch theme" }).click();
    await expect.poll(panelBackground).not.toBe(initialBackground);
    await page.screenshot({
      path: testInfo.outputPath("toolbar-other-theme.png"),
    });
    await page.getByRole("button", { name: "Export ZIP", exact: true }).click();
    await expect
      .poll(() =>
        worker.evaluate(
          async () =>
            (await chrome.downloads.search({ state: "complete" })).length,
        ),
      )
      .toBe(2);
    const secondFile = await worker.evaluate(
      async () =>
        (await chrome.downloads.search({ orderBy: ["-startTime"] }))[0]
          .filename,
    );
    const complex = decodeBundle(await readFile(secondFile));
    expect(complex.manifest.annotations).toHaveLength(5);
    const frameNote = complex.manifest.annotations.find(
      (a) => a.comment === "Scaled preview target",
    )!;
    expect(frameNote.frame.path).toEqual(["#preview"]);
    expect(frameNote.target.bounds).toEqual({
      x: 20,
      y: 50,
      width: 100,
      height: 40,
    });
    expect(frameNote.target.screenshotBounds).toEqual({
      x: 410,
      y: 125,
      width: 50,
      height: 20,
    });
    const shadowNote = complex.manifest.annotations.find(
      (a) => a.comment === "Open shadow target",
    )!;
    expect(shadowNote.target.locator).toEqual([
      "#open-shadow",
      "#shadow-action",
    ]);
    expect(
      complex.manifest.annotations.find(
        (a) => a.comment === "Restricted frame host",
      )!.frame.access,
    ).toBe("host-only");
    expect(
      complex.manifest.annotations.find((a) => a.comment === "Touch region")!
        .target.bounds,
    ).toEqual({ x: 300, y: 300, width: 80, height: 50 });
    await page
      .locator("#gobby-annotate-root")
      .evaluate((node) => node.dispatchEvent(new Event("annotate-deactivate")));
    await expect(page.locator("#gobby-annotate-root")).toHaveCount(0);
    await page.locator("#target").click();
    await expect(page.locator("#target")).toHaveText("activated");
    expect(errors).toEqual([]);
  } finally {
    await context.close();
    await rm(temporary, { recursive: true, force: true });
  }
});
