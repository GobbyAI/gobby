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

test.describe("File editor", () => {
  test.beforeEach(async ({ page }) => {
    await setupApiMocks(page);
    await page.goto("/");
  });

  test("can navigate to Files, open a file, and click Edit", async ({
    page,
  }) => {
    // 1. Click the hamburger to open sidebar, then click Files nav item
    await page.click(".hamburger-button");
    await page.click('.sidebar-item:has-text("Files")');

    // 2. Wait for the files page and project to appear
    await page.waitForSelector(".files-page");
    await page.waitForSelector(".files-project-header");

    // 3. Expand the project
    await page.click(".files-project-header");
    await page.waitForSelector(".files-project-children");

    // 4. Expand the src directory
    await page.click('.files-tree-item:has-text("src")');
    await page.waitForSelector('.files-tree-file:has-text("main.py")');

    // 5. Click a file to open it
    await page.click('.files-tree-file:has-text("main.py")');

    // 6. Wait for the file tab and toolbar to appear (file loaded)
    await page.waitForSelector(".files-tab.active");
    await page.waitForSelector(".files-toolbar");

    // Verify the file name shows in the tab
    await expect(page.locator(".files-tab.active .files-tab-name")).toHaveText(
      "main.py",
    );

    // Verify the toolbar shows the file path
    await expect(page.locator(".files-toolbar-path")).toHaveText("src/main.py");

    // Verify code is rendered in the read-only syntax highlighter
    await expect(page.locator(".files-code-viewer")).toBeVisible();

    // Verify the Edit button says "Edit" (not yet in editing mode)
    const editButton = page.locator(".files-edit-toggle");
    await expect(editButton).toBeVisible();
    await expect(editButton).toHaveText("Edit");

    // 7. Click the Edit button
    await editButton.click();

    // 8. Verify we're now in edit mode
    await expect(editButton).toHaveText("View");
    await expect(editButton).toHaveClass(/active/);

    // Verify the CodeMirror editor appeared
    await page.waitForSelector(".cm-editor");
    await expect(page.locator(".cm-editor")).toBeVisible();

    // Take a screenshot for visual reference
    await page.screenshot({
      path: "tests/screenshots/file-editor-edit-mode.png",
      fullPage: true,
    });
  });

  test("Edit button toggles back to View mode", async ({ page }) => {
    // Navigate to files and open a file
    await page.click(".hamburger-button");
    await page.click('.sidebar-item:has-text("Files")');
    await page.waitForSelector(".files-project-header");
    await page.click(".files-project-header");
    await page.waitForSelector(".files-project-children");
    await page.click('.files-tree-item:has-text("src")');
    await page.waitForSelector('.files-tree-file:has-text("main.py")');
    await page.click('.files-tree-file:has-text("main.py")');
    await page.waitForSelector(".files-edit-toggle");

    const editButton = page.locator(".files-edit-toggle");

    // Enter edit mode
    await editButton.click();
    await expect(editButton).toHaveText("View");
    await page.waitForSelector(".cm-editor");

    // Toggle back to view mode
    await editButton.click();
    await expect(editButton).toHaveText("Edit");

    // CodeMirror editor should be gone, syntax highlighter should be back
    await expect(page.locator(".cm-editor")).not.toBeVisible();
    await expect(page.locator(".files-code-viewer")).toBeVisible();
  });

  test("can open multiple files without fetch errors", async ({ page }) => {
    // Navigate to files and expand the tree
    await page.click(".hamburger-button");
    await page.click('.sidebar-item:has-text("Files")');
    await page.waitForSelector(".files-project-header");
    await page.click(".files-project-header");
    await page.waitForSelector(".files-project-children");
    await page.click('.files-tree-item:has-text("src")');
    await page.waitForSelector('.files-tree-file:has-text("main.py")');

    // Open first file
    await page.click('.files-tree-file:has-text("main.py")');
    await page.waitForSelector(".files-tab.active");
    await page.waitForSelector(".files-toolbar");
    await expect(page.locator(".files-tab.active .files-tab-name")).toHaveText(
      "main.py",
    );

    // Verify no errors on first file
    await expect(page.locator(".files-viewer-error")).not.toBeVisible();

    // Open second file
    await page.click('.files-tree-file:has-text("utils.ts")');
    await page.waitForSelector(
      '.files-tab.active .files-tab-name:has-text("utils.ts")',
    );

    // Verify no errors on second file
    await expect(page.locator(".files-viewer-error")).not.toBeVisible();
    await expect(page.locator(".files-code-viewer")).toBeVisible();

    // Should have 2 tabs
    const tabs = page.locator(".files-tab");
    await expect(tabs).toHaveCount(2);

    // Open third file (README.md from root)
    await page.click('.files-tree-file:has-text("README.md")');
    await page.waitForSelector(
      '.files-tab.active .files-tab-name:has-text("README.md")',
    );

    // Verify no errors on third file
    await expect(page.locator(".files-viewer-error")).not.toBeVisible();
    await expect(page.locator(".files-code-viewer")).toBeVisible();

    // Should have 3 tabs
    await expect(tabs).toHaveCount(3);

    // Click back to first file tab - should still work
    await page.click('.files-tab:has-text("main.py")');
    await expect(
      page.locator('.files-tab.active .files-tab-name:has-text("main.py")'),
    ).toBeVisible();
    await expect(page.locator(".files-viewer-error")).not.toBeVisible();
  });
});
