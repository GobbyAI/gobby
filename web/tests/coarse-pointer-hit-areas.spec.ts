import { expect, test, type Locator } from "@playwright/test";

/**
 * Effective-activation proof for the invisible 44×44 coarse-pointer hit
 * areas (plan web-styling-consolidation-phase-2 §3.3): under an emulated
 * coarse pointer, clicks at the expanded perimeter of Input, Textarea,
 * NativeSelect, and the Radix Select trigger/items activate or focus the
 * control while visible geometry is unchanged. JSDOM computed-box tests
 * (src/__tests__/coarsePointerTouchTargets.test.ts) pin the CSS geometry;
 * this spec pins real hit-testing, which pseudo-elements make unprovable in
 * JSDOM. Runs against the dev harness (`/?hit-area-harness`), which renders
 * each primitive at its bare 36px ladder size.
 */

// Spec-level coarse-pointer axis — the same touch descriptor the 1.3
// capture matrix asserts (project style-capture-coarse).
test.use({ hasTouch: true });

const LADDER_HEIGHT = 36;
// The expansion floors the hit target at 44px, so clicks up to 4px outside a
// 36px control must still land. Click 3px out to stay inside the expansion.
const PERIMETER_OFFSET = 3;

test.beforeEach(async ({ page }) => {
  await page.goto("/?hit-area-harness");
  await expect(page.getByTestId("harness-input")).toBeVisible();
  expect(
    await page.evaluate(() => window.matchMedia("(pointer: coarse)").matches),
  ).toBe(true);
});

async function assertUnchangedGeometry(control: Locator) {
  const box = (await control.boundingBox())!;
  expect(box.height).toBe(LADDER_HEIGHT);
  return box;
}

async function assertWrapperExpansion(control: Locator) {
  // The wrapper label's ::before is the hit surface: centered, ≥44px floors.
  const pseudo = await control.evaluate((element) => {
    const style = getComputedStyle(element.closest("label")!, "::before");
    return {
      position: style.position,
      minWidth: style.minWidth,
      minHeight: style.minHeight,
    };
  });
  expect(pseudo).toEqual({
    position: "absolute",
    minWidth: "44px",
    minHeight: "44px",
  });
}

test("input focuses from the expanded perimeter", async ({ page }) => {
  const input = page.getByTestId("harness-input");
  const box = await assertUnchangedGeometry(input);
  await assertWrapperExpansion(input);
  await page.mouse.click(box.x + box.width / 2, box.y - PERIMETER_OFFSET);
  await expect(input).toBeFocused();
});

test("textarea focuses from the expanded perimeter", async ({ page }) => {
  const textarea = page.getByTestId("harness-textarea");
  const box = await assertUnchangedGeometry(textarea);
  await assertWrapperExpansion(textarea);
  await page.mouse.click(
    box.x + box.width / 2,
    box.y + box.height + PERIMETER_OFFSET,
  );
  await expect(textarea).toBeFocused();
});

test("native select focuses from the expanded perimeter", async ({ page }) => {
  const select = page.getByTestId("harness-native-select");
  const box = await assertUnchangedGeometry(select);
  await assertWrapperExpansion(select);
  await page.mouse.click(box.x + box.width / 2, box.y - PERIMETER_OFFSET);
  await expect(select).toBeFocused();
});

test("radix trigger opens and an item commits from the expanded perimeter", async ({
  page,
}) => {
  const trigger = page.getByTestId("harness-radix-trigger");
  const triggerBox = await assertUnchangedGeometry(trigger);
  // The trigger hosts its ::before directly — no wrapper involved.
  const pseudo = await trigger.evaluate((element) => {
    const style = getComputedStyle(element, "::before");
    return { position: style.position, minHeight: style.minHeight };
  });
  expect(pseudo).toEqual({ position: "absolute", minHeight: "44px" });

  await page.mouse.click(
    triggerBox.x + triggerBox.width / 2,
    triggerBox.y - PERIMETER_OFFSET,
  );
  const listbox = page.getByRole("listbox");
  await expect(listbox).toBeVisible();

  // The last item's expansion extends past its own box with nothing painted
  // over it; a click below the visible box must still commit the value.
  const lastItem = page.getByRole("option", { name: "Gamma" });
  const itemBox = (await lastItem.boundingBox())!;
  expect(itemBox.height).toBeLessThan(44);
  await page.mouse.click(
    itemBox.x + itemBox.width / 2,
    itemBox.y + itemBox.height + PERIMETER_OFFSET,
  );
  await expect(listbox).toBeHidden();
  await expect(trigger).toContainText("Gamma");
});
