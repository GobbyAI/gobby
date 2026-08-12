import { test, expect } from "@playwright/test";

const CONVERSATION_ID = "test-conversation-id";
const STORAGE_KEY = `gobby-chat-${CONVERSATION_ID}`;
const CONVERSATION_ID_KEY = "gobby-conversation-id";

const markdownFixture = `# Heading 1
## Heading 2
### Heading 3

Regular paragraph with **bold**, *italic*, ~~strikethrough~~, and \`inline code\`.

> This is a blockquote
> with multiple lines

\`\`\`python
def hello():
    print("world")
\`\`\`

| Header 1 | Header 2 |
|----------|----------|
| Cell 1   | Cell 2   |

- Item 1
- Item 2
  - Nested item

1. First
2. Second

---

- [x] Done task
- [ ] Pending task

[Link text](https://example.com)`;

function makeStoredMessages(content: string) {
  return JSON.stringify([
    {
      id: "test-user-1",
      role: "user",
      content: "Show me markdown",
      timestamp: new Date().toISOString(),
    },
    {
      id: "test-assistant-1",
      role: "assistant",
      content,
      timestamp: new Date().toISOString(),
    },
  ]);
}

test.describe("Markdown rendering", () => {
  test.beforeEach(async ({ page }) => {
    // Inject test messages into localStorage before the app loads
    // Uses per-conversation storage keys (gobby-chat-<id>) and conversation ID key
    await page.addInitScript(
      ({ messages, convId, storageKey, convIdKey }) => {
        localStorage.setItem(convIdKey, convId);
        localStorage.setItem(storageKey, messages);
      },
      {
        messages: makeStoredMessages(markdownFixture),
        convId: CONVERSATION_ID,
        storageKey: STORAGE_KEY,
        convIdKey: CONVERSATION_ID_KEY,
      },
    );
    await page.goto("/");
    // Wait for the message content to render
    await page.waitForSelector(".message-content");
  });

  test("renders headings with correct hierarchy", async ({ page }) => {
    const content = page.locator(".message-assistant .message-content").first();
    await expect(content.locator("h1")).toHaveText("Heading 1");
    await expect(content.locator("h2")).toHaveText("Heading 2");
    await expect(content.locator("h3")).toHaveText("Heading 3");

    // h1 should be larger than h2
    const h1Size = await content
      .locator("h1")
      .evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
    const h2Size = await content
      .locator("h2")
      .evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
    expect(h1Size).toBeGreaterThan(h2Size);
  });

  test("renders inline formatting correctly", async ({ page }) => {
    const content = page.locator(".message-assistant .message-content").first();

    await expect(content.locator("strong")).toContainText("bold");
    await expect(content.locator("em")).toContainText("italic");
    await expect(content.locator("del")).toContainText("strikethrough");

    // Inline code should have monospace font and background
    const inlineCode = content
      .locator("code")
      .filter({ hasText: "inline code" })
      .first();
    await expect(inlineCode).toBeVisible();
    const bg = await inlineCode.evaluate(
      (el) => getComputedStyle(el).backgroundColor,
    );
    expect(bg).not.toBe("rgba(0, 0, 0, 0)");
  });

  test("renders code blocks with syntax highlighting", async ({ page }) => {
    const codeBlock = page.locator(".code-block-wrapper").first();
    await expect(codeBlock).toBeVisible();

    // Should show language label
    await expect(codeBlock.locator(".code-block-language")).toHaveText(
      "python",
    );

    // Should have a copy button
    await expect(codeBlock.locator(".code-block-copy")).toBeVisible();
  });

  test("renders blockquotes with left border", async ({ page }) => {
    const blockquote = page
      .locator(".message-assistant .message-content blockquote")
      .first();
    await expect(blockquote).toBeVisible();
    await expect(blockquote).toContainText("This is a blockquote");

    const borderLeft = await blockquote.evaluate(
      (el) => getComputedStyle(el).borderLeftStyle,
    );
    expect(borderLeft).toBe("solid");
  });

  test("renders tables with proper structure", async ({ page }) => {
    const content = page.locator(".message-assistant .message-content").first();

    // Table should be wrapped in .table-wrapper for horizontal scrolling
    const wrapper = content.locator(".table-wrapper").first();
    await expect(wrapper).toBeVisible();

    const table = wrapper.locator("table");
    await expect(table.locator("th").first()).toHaveText("Header 1");
    await expect(table.locator("td").first()).toHaveText("Cell 1");
  });

  test("renders lists with proper nesting", async ({ page }) => {
    const content = page.locator(".message-assistant .message-content").first();

    // Unordered list
    const ul = content.locator("ul").first();
    await expect(ul).toBeVisible();

    // Ordered list
    const ol = content.locator("ol").first();
    await expect(ol).toBeVisible();
  });

  test("renders horizontal rule", async ({ page }) => {
    const hr = page.locator(".message-assistant .message-content hr").first();
    await expect(hr).toBeVisible();
  });

  test("renders task list checkboxes", async ({ page }) => {
    const content = page.locator(".message-assistant .message-content").first();
    const checkboxes = content.locator('input[type="checkbox"]');
    expect(await checkboxes.count()).toBeGreaterThanOrEqual(2);

    // First checkbox should be checked, second unchecked
    await expect(checkboxes.first()).toBeChecked();
    await expect(checkboxes.nth(1)).not.toBeChecked();
  });

  test("renders links with target=_blank for external URLs", async ({
    page,
  }) => {
    const link = page
      .locator(
        '.message-assistant .message-content a[href="https://example.com"]',
      )
      .first();
    await expect(link).toBeVisible();
    await expect(link).toHaveText("Link text");
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", /noopener/);
  });

  test("takes screenshot for visual comparison", async ({ page }) => {
    await page.screenshot({
      path: "tests/screenshots/markdown-rendering.png",
      fullPage: true,
    });
  });
});
