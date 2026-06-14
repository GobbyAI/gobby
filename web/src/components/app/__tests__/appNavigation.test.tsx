import { describe, expect, it } from "vitest";

import { APP_NAV_PAGES, APP_VALID_TABS, createAppNavItems } from "../appNavigation";

describe("app navigation", () => {
  it("does not expose the retired Skills page route", () => {
    expect(APP_VALID_TABS.has("skills")).toBe(false);
    expect(APP_NAV_PAGES.map((page) => page.id)).not.toContain("skills");
    expect(createAppNavItems().map((item) => item.id)).not.toContain("skills");
  });

  it("does not expose the retired Integrations page route", () => {
    expect(APP_VALID_TABS.has("integrations")).toBe(false);
    expect(APP_NAV_PAGES.map((page) => page.id)).not.toContain("integrations");
    expect(createAppNavItems().map((item) => item.id)).not.toContain("integrations");
  });
});
