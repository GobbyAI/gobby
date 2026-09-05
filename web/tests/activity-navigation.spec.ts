import { expect, test } from "@playwright/test";

const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "portrait", width: 440, height: 956 },
  { name: "landscape", width: 932, height: 430 },
];

for (const theme of ["dark", "light"] as const) {
  for (const viewport of viewports) {
    for (const hasTouch of [false, true]) {
      test(`${theme} ${viewport.name} ${hasTouch ? "coarse" : "fine"} navigation fits`, async ({
        browser,
      }, testInfo) => {
        const context = await browser.newContext({
          viewport,
          hasTouch,
          colorScheme: theme,
          baseURL: testInfo.project.use.baseURL,
        });
        try {
          const page = await context.newPage();
          const errors: string[] = [];
          page.on("pageerror", (error) => errors.push(error.message));
          await page.addInitScript((theme) => {
            localStorage.setItem("gobby-settings", JSON.stringify({ theme }));
            localStorage.setItem("gobby-activity-panel-tab-v2", "sessions");
            localStorage.setItem("gobby-activity-panel-layout", "panel");
          }, theme);
          await page.routeWebSocket("**/ws", () => {});
          await page.route(
            (url) => url.pathname.startsWith("/api/"),
            async (route) => {
              const responses: Record<string, unknown> = {
                "/api/auth/status": { authenticated: true },
                "/api/config/ui-settings": { theme },
                "/api/projects": [],
                "/api/providers": { providers: [] },
                "/api/sessions": { sessions: [], total: 0, next_cursor: null },
                "/api/agents/running": { agents: [] },
                "/api/memories": { memories: [] },
              };
              await route.fulfill({
                json: responses[new URL(route.request().url()).pathname] ?? {},
              });
            },
          );
          await page.goto("/");
          await page
            .getByRole("button", { name: "Sessions", exact: true })
            .click();
          const menu = page.locator(".activity-panel-mobile-menu");
          await expect(menu).toBeVisible();
          const geometry = await menu.evaluate((element) => {
            const bounds = element.getBoundingClientRect();
            return {
              left: bounds.left,
              right: bounds.right,
              bottom: bounds.bottom,
              overflow: element.scrollHeight > element.clientHeight,
              items: Array.from(element.querySelectorAll("button")).map(
                (button) => {
                  const rect = button.getBoundingClientRect();
                  return {
                    label: button.textContent?.trim(),
                    left: rect.left,
                    top: rect.top,
                    bottom: rect.bottom,
                    height: rect.height,
                  };
                },
              ),
            };
          });
          expect(geometry.overflow).toBe(false);
          expect(geometry.left).toBeGreaterThanOrEqual(0);
          expect(geometry.right).toBeLessThanOrEqual(viewport.width);
          expect(geometry.bottom).toBeLessThanOrEqual(viewport.height);
          expect(geometry.items.length).toBeGreaterThan(10);
          for (const item of geometry.items) {
            expect(item.bottom).toBeLessThanOrEqual(geometry.bottom);
            expect(item.height).toBeGreaterThanOrEqual(hasTouch ? 44 : 24);
          }
          const columnOrder = [...geometry.items].sort(
            (a, b) => a.left - b.left || a.top - b.top,
          );
          expect(columnOrder.map((item) => item.label)).toEqual(
            geometry.items.map((item) => item.label).sort(),
          );
          await page.screenshot({
            path: testInfo.outputPath("navigation.png"),
          });
          await menu
            .getByRole("button", { name: "Memory", exact: true })
            .focus();
          await page.keyboard.press("Enter");
          await expect(
            page.getByRole("complementary", { name: "Activity: Memory" }),
          ).toBeVisible();
          expect(errors).toEqual([]);
        } finally {
          await context.close();
        }
      });
    }
  }
}
