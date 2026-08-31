import { test, expect } from "@playwright/test";

// Mock data for API responses
const mockProjects = [
  {
    id: "proj-1",
    name: "test-project",
    checkout: {
      machine_id: "machine-1",
      root_path: "/tmp/test-project",
    },
  },
];

const mockRootTree = [
  { name: "src", path: "src", is_dir: true, extension: null, size: null },
  {
    name: "README.md",
    path: "README.md",
    is_dir: false,
    extension: ".md",
    size: 256,
  },
];

const mockSrcTree = [
  {
    name: "main.py",
    path: "src/main.py",
    is_dir: false,
    extension: ".py",
    size: 1024,
  },
  {
    name: "utils.ts",
    path: "src/utils.ts",
    is_dir: false,
    extension: ".ts",
    size: 512,
  },
];

const mockFileContent = {
  content:
    'def hello():\n    print("Hello, world!")\n\nif __name__ == "__main__":\n    hello()\n',
  image: false,
  binary: false,
  mime_type: "text/x-python",
  size: 78,
};

const mockGitStatus = {
  branch: "main",
  files: {},
};

function setupApiMocks(page: import("@playwright/test").Page) {
  page.route("**/api/auth/status", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true }),
    });
  });

  page.route("**/api/projects", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockProjects),
    });
  });

  // Mock projects endpoint
  page.route("**/api/files/projects", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockProjects),
    });
  });

  // Mock tree endpoint
  page.route("**/api/files/tree*", (route) => {
    const url = new URL(route.request().url());
    const path = url.searchParams.get("path") || "";

    const entries =
      path === "" ? mockRootTree : path === "src" ? mockSrcTree : [];

    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(entries),
    });
  });

  // Mock file read endpoint
  page.route("**/api/files/read*", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockFileContent),
    });
  });

  // Mock git status endpoint
  page.route("**/api/files/git-status*", (route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(mockGitStatus),
    });
  });

  // Mock WebSocket connection to prevent errors
  page.route("**/ws", (route) => route.abort());
}

async function openFilesTab(page: import("@playwright/test").Page) {
  await page.locator(".activity-panel-mobile-trigger").click();
  await page
    .locator(".activity-panel-mobile-menu")
    .getByRole("button", { name: "Files", exact: true })
    .click();

  const panel = page.getByRole("complementary", { name: "Activity: Files" });
  await expect(
    panel.getByRole("tree", { name: "Project files" }),
  ).toBeVisible();
  return panel;
}

async function openSrcFile(
  panel: import("@playwright/test").Locator,
  fileName: RegExp,
) {
  const tree = panel.getByRole("tree", { name: "Project files" });
  const src = tree.getByRole("treeitem", { name: /^src\b/ });
  if ((await src.getAttribute("aria-expanded")) !== "true") {
    await src.click();
  }
  await tree.getByRole("treeitem", { name: fileName }).click();
}

test.describe("File editor", () => {
  test.beforeEach(async ({ page }) => {
    await setupApiMocks(page);
    await page.goto("/");
  });

  test("can navigate to Files, open a file, and click Edit", async ({
    page,
  }) => {
    // 1. Open Files from the activity-panel tab picker.
    const panel = await openFilesTab(page);
    await openSrcFile(panel, /^main\.py\b/);

    await expect(panel.getByText("src/main.py", { exact: true })).toBeVisible();
    await expect(panel.locator("code")).toContainText("def hello");

    const editButton = panel.getByRole("button", { name: "Edit", exact: true });
    await editButton.click();

    await expect(panel.getByRole("button", { name: "Save" })).toBeVisible();
    await expect(panel.getByRole("button", { name: "Cancel" })).toBeVisible();
    await expect(panel.locator(".cm-editor")).toBeVisible();
  });

  test("Cancel returns the editor to read-only mode", async ({ page }) => {
    const panel = await openFilesTab(page);
    await openSrcFile(panel, /^main\.py\b/);

    const editButton = panel.getByRole("button", { name: "Edit", exact: true });
    await editButton.click();
    await panel.getByRole("button", { name: "Cancel" }).click();

    await expect(editButton).toBeVisible();
    await expect(panel.locator(".cm-editor")).toHaveCount(0);
    await expect(panel.locator("code")).toContainText("def hello");
  });

  test("can open multiple files without fetch errors", async ({ page }) => {
    const panel = await openFilesTab(page);

    await openSrcFile(panel, /^main\.py\b/);
    await expect(panel.getByText("src/main.py", { exact: true })).toBeVisible();

    await openSrcFile(panel, /^utils\.ts\b/);
    await expect(
      panel.getByText("src/utils.ts", { exact: true }),
    ).toBeVisible();

    const readme = panel
      .getByRole("tree", { name: "Project files" })
      .getByRole("treeitem", { name: /^README\.md\b/ });
    await readme.click();
    await expect(readme).toHaveAttribute("aria-selected", "true");
    await expect(panel.getByRole("alert")).toHaveCount(0);
  });
});
