import { expect, test } from "@playwright/test";

const CURRENT_CONVERSATION_ID = "web-provider-picker-conv";

interface MockApiOptions {
  onWebChatSessionCreate?: (body: Record<string, unknown>) => void;
}

function mockApiRoutes(
  page: Parameters<typeof test>[0]["page"],
  options: MockApiOptions = {},
) {
  return page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();

    if (path === "/api/auth/status") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
      return;
    }

    if (path === "/api/config/ui-settings") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body:
          method === "PUT"
            ? JSON.stringify({ ok: true })
            : JSON.stringify({
                selectedProjectId: "proj-1",
                model: "opus",
                theme: "dark",
                defaultChatMode: "plan",
                fontSize: 16,
              }),
      });
      return;
    }

    if (path === "/api/providers") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          providers: [
            { name: "claude", available: true },
            { name: "codex", available: true },
          ],
        }),
      });
      return;
    }

    if (path === "/api/providers/models") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          providers: [
            {
              provider: "claude",
              available: true,
              models: [
                {
                  value: "opus",
                  label: "Opus",
                  canonical_id: "claude-opus-4-6",
                },
                {
                  value: "sonnet",
                  label: "Sonnet",
                  canonical_id: "claude-sonnet-4-6",
                },
                {
                  value: "haiku",
                  label: "Haiku",
                  canonical_id: "claude-haiku-4-5-20251001",
                },
              ],
              source: "static",
            },
            {
              provider: "codex",
              available: true,
              models: [
                { value: "gpt-5.4", label: "GPT 5.4" },
                { value: "gpt-5.4-mini", label: "GPT 5.4 Mini" },
                { value: "gpt-5.3-codex", label: "GPT 5.3 Codex" },
                { value: "gpt-5.3-codex-spark", label: "GPT 5.3 Codex Spark" },
              ],
              source: "static",
            },
            {
              provider: "endpoint:studio",
              display_name: "LM Studio",
              available: true,
              models: [
                {
                  value: "endpoint:studio/qwen3-coder",
                  label: "Qwen3 Coder",
                },
              ],
              source: "live",
              execution_provider: "codex",
              supports_web_chat: true,
            },
            {
              provider: "endpoint:generic",
              display_name: "OpenAI Compatible",
              available: false,
              models: [
                {
                  value: "endpoint:generic/fallback-model",
                  label: "Generic Fallback",
                },
              ],
              source: "config",
              supports_web_chat: false,
              unavailable_reason:
                "Generic OpenAI-compatible endpoints are unavailable for web chat",
            },
          ],
        }),
      });
      return;
    }

    if (path === "/api/sessions/web-chat" && method === "POST") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      options.onWebChatSessionCreate?.(body);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: {
            id: "local-provider-session",
            source: body.provider,
            model: body.model,
            chat_mode: body.chat_mode ?? "plan",
            seq_num: 1,
            title: null,
            status: "active",
            session_type: "web_chat",
          },
        }),
      });
      return;
    }

    if (path === "/api/voice/status") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false, stt_available: false }),
      });
      return;
    }

    if (path === "/api/projects") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "proj-1",
            display_name: "Project One",
            checkout: {
              machine_id: "machine-1",
              root_path: "/tmp/project-one",
            },
            github_repo: null,
            session_count: 0,
            open_task_count: 0,
          },
        ]),
      });
      return;
    }

    if (path === "/api/files/projects") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "proj-1",
            name: "Project One",
            checkout: {
              machine_id: "machine-1",
              root_path: "/tmp/project-one",
            },
          },
        ]),
      });
      return;
    }

    if (path === "/api/agents/running") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ agents: [] }),
      });
      return;
    }

    if (path === "/api/agents/definitions") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          definitions: [],
          global_defs: [],
          project_defs: [],
        }),
      });
      return;
    }

    if (path === "/api/sessions") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ sessions: [], total: 0 }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({}),
    });
  });
}

async function prepareProviderPickerPage(
  page: Parameters<typeof test>[0]["page"],
) {
  await page.addInitScript(
    ({ conversationId }: { conversationId: string }) => {
      localStorage.setItem("gobby-conversation-id", conversationId);
      localStorage.removeItem("gobby-db-session-id");
      localStorage.removeItem("gobby-selected-provider");
      localStorage.setItem(
        "gobby-settings",
        JSON.stringify({
          model: "opus",
          fontSize: 16,
          theme: "dark",
          defaultChatMode: "plan",
        }),
      );
    },
    { conversationId: CURRENT_CONVERSATION_ID },
  );

  await mockApiRoutes(page);
}

test("provider chip repairs an invalid persisted provider/model pair", async ({
  page,
}) => {
  await prepareProviderPickerPage(page);
  await page.addInitScript(() => {
    localStorage.setItem("gobby-selected-provider", "claude");
    localStorage.setItem(
      "gobby-settings",
      JSON.stringify({
        model: "gpt-5.4",
        fontSize: 16,
        theme: "dark",
        defaultChatMode: "plan",
      }),
    );
  });

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
      }
    });
  });

  await page.goto("/#chat");

  const providerButton = page.getByLabel("Select provider");
  const modelButton = page.getByLabel("Select model");
  await expect(providerButton).toBeVisible();
  await expect(providerButton).toHaveAttribute("title", "Claude");
  await expect(modelButton).not.toContainText("GPT 5.4");
  await expect(modelButton).toContainText("Opus");
});

test("provider chip normalizes canonical model ids to the matching alias", async ({
  page,
}) => {
  await prepareProviderPickerPage(page);
  await page.addInitScript(() => {
    localStorage.setItem("gobby-selected-provider", "claude");
    localStorage.setItem(
      "gobby-settings",
      JSON.stringify({
        model: "claude-opus-4-6",
        fontSize: 16,
        theme: "dark",
        defaultChatMode: "plan",
      }),
    );
  });

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
      }
    });
  });

  await page.goto("/#chat");

  const providerButton = page.getByLabel("Select provider");
  const modelButton = page.getByLabel("Select model");
  await expect(providerButton).toBeVisible();
  await expect(providerButton).toHaveAttribute("title", "Claude");
  await expect(modelButton).not.toContainText("Haiku");
  await expect(modelButton).toContainText("Opus");
});

test("Codex picker shows friendly labels and no Default placeholder", async ({
  page,
}) => {
  await prepareProviderPickerPage(page);
  const outboundMessages: Array<Record<string, unknown>> = [];

  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      outboundMessages.push(msg);

      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
        return;
      }

      if (msg.type === "set_provider") {
        ws.send(
          JSON.stringify({
            type: "provider_switched",
            conversation_id: msg.conversation_id,
            old_provider: null,
            provider: msg.provider,
          }),
        );
      }
    });
  });

  await page.goto("/#chat");
  await expect(
    page.getByRole("textbox", { name: /message input/i }),
  ).toBeVisible();

  await page.getByLabel("Select provider").click();
  await expect(page.getByText("Codex", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "GPT 5.4", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "GPT 5.4 Mini", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "GPT 5.3 Codex", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "GPT 5.3 Codex Spark", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Default", exact: true }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "GPT 5.4", exact: true }).click();

  await expect(page.getByLabel("Select provider")).toHaveAttribute(
    "title",
    "Codex",
  );
  await expect(page.getByLabel("Select model")).toContainText("GPT 5.4");
});

test("local catalog selection routes through Codex and restores its catalog owner", async ({
  page,
}) => {
  const createdSessions: Array<Record<string, unknown>> = [];
  await page.addInitScript(
    ({ conversationId }: { conversationId: string }) => {
      localStorage.setItem("gobby-conversation-id", conversationId);
      localStorage.removeItem("gobby-db-session-id");
      localStorage.removeItem("gobby-selected-provider");
      localStorage.setItem(
        "gobby-settings",
        JSON.stringify({
          model: "opus",
          fontSize: 16,
          theme: "dark",
          defaultChatMode: "plan",
        }),
      );
    },
    { conversationId: CURRENT_CONVERSATION_ID },
  );
  await mockApiRoutes(page, {
    onWebChatSessionCreate: (body) => createdSessions.push(body),
  });
  await page.routeWebSocket("**/ws", (ws) => {
    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as Record<string, unknown>;
      if (msg.type === "subscribe") {
        ws.send(
          JSON.stringify({
            type: "connection_established",
            conversation_ids: [CURRENT_CONVERSATION_ID],
          }),
        );
        ws.send(
          JSON.stringify({
            type: "subscribe_success",
            events: msg.events ?? [],
          }),
        );
      }
    });
  });

  await page.goto("/#chat");
  const messageInput = page.getByRole("textbox", { name: /message input/i });
  await expect(messageInput).toBeVisible();

  await page.getByLabel("Select provider").click();
  await expect(page.getByText("LM Studio", { exact: true })).toBeVisible();
  await expect(
    page.getByText("OpenAI Compatible", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Generic Fallback", exact: true }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Qwen3 Coder", exact: true }).click();
  await expect(page.getByLabel("Select provider")).toHaveAttribute(
    "title",
    "Codex",
  );
  await expect(page.getByLabel("Select model")).toContainText("Qwen3 Coder");

  await messageInput.fill("Exercise the local provider contract");
  await messageInput.press("Enter");
  await expect.poll(() => createdSessions).toHaveLength(1);
  expect(createdSessions[0]).toMatchObject({
    provider: "codex",
    model: "endpoint:studio/qwen3-coder",
  });

  await page.getByLabel("Select provider").click();
  const lmStudioHeader = page
    .getByText("LM Studio", { exact: true })
    .locator("..");
  await expect(
    lmStudioHeader.getByText("active", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Codex", { exact: true }).locator("..").getByText("active", {
      exact: true,
    }),
  ).toHaveCount(0);
});
