import { expect, test } from "@playwright/test";

test.use({
  // test-quality: allow NO_ASSERTION -- Playwright fixture configuration, not a test case
  hasTouch: true,
  isMobile: true,
  viewport: { width: 1024, height: 768 },
});

const targets = [
  "primary action",
  "queued-file remove",
  "artifact open",
  "code copy",
  "session actions",
  "task actions",
  "task expand",
  "activity menu item",
  "filter option",
  "session role filter",
  "error dismiss",
] as const;

test("coarse pointers promote compact chat and activity controls to 44px", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect
    .poll(() => page.evaluate(() => matchMedia("(pointer: coarse)").matches))
    .toBe(true);

  await page.locator("body").evaluate((body) => {
    body.innerHTML = `
      <button data-target="primary action" class="h-[36px] w-[36px] pointer-coarse:h-11 pointer-coarse:w-11">Send</button>
      <button data-target="queued-file remove" class="h-4 w-4 pointer-coarse:h-11 pointer-coarse:w-11">×</button>
      <button data-target="artifact open" class="pointer-coarse:min-h-11 pointer-coarse:min-w-11">Open</button>
      <button data-target="code copy" class="pointer-coarse:min-h-11 pointer-coarse:min-w-11">Copy</button>
      <button data-target="session actions" class="session-more-btn">⋮</button>
      <button data-target="task actions" class="task-more-btn">⋮</button>
      <button data-target="task expand" class="activity-task-row-toggle">›</button>
      <button data-target="activity menu item" class="activity-panel-mobile-menu__item">Sessions</button>
      <label data-target="filter option" class="flex pointer-coarse:min-h-11"><input type="checkbox"> Filter</label>
      <label data-target="session role filter" class="flex pointer-coarse:min-h-11"><input type="checkbox"> Parent</label>
      <button data-target="error dismiss" class="activity-task-detail-edit-error__dismiss">×</button>
    `;
  });

  for (const target of targets) {
    const size = await page.locator(`[data-target="${target}"]`).evaluate((element) => {
      const style = getComputedStyle(element);
      return { height: parseFloat(style.height), width: parseFloat(style.width) };
    });

    expect(size.width, `${target} width`).toBeGreaterThanOrEqual(44);
    expect(size.height, `${target} height`).toBeGreaterThanOrEqual(44);
  }
});
