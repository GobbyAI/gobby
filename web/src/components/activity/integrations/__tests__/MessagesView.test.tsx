import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Channel, CommsMessage } from "../../../../hooks/useIntegrations";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../../test/mocks/fetch";
import { MessagesView } from "../MessagesView";

let mockFetch: MockFetchInstance;

const channel: Channel = {
  id: "ch-slack",
  channel_type: "slack",
  name: "Release alerts",
  enabled: true,
  config_json: {},
  webhook_secret: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

function message(overrides: Partial<CommsMessage>): CommsMessage {
  return {
    id: "msg-default",
    channel_id: channel.id,
    identity_id: null,
    direction: "inbound",
    content: "Default message",
    content_type: "text",
    platform_message_id: null,
    platform_thread_id: null,
    session_id: null,
    status: "delivered",
    error: null,
    metadata_json: {},
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setupFetch(responses: CommsMessage[][]) {
  mockFetch = createMockFetch();
  let callIndex = 0;
  mockFetch.fn.mockImplementation(async (input: RequestInfo | URL) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    if (url.includes("/api/comms/messages")) {
      const response =
        responses[Math.min(callIndex, responses.length - 1)] ?? [];
      callIndex += 1;
      return jsonResponse(response);
    }
    return jsonResponse({ error: "no mock route matched" }, 404);
  });
}

function requestUrls() {
  return mockFetch.fn.mock.calls.map(([url]) => String(url));
}

describe("MessagesView", () => {
  afterEach(() => {
    mockFetch?.restore();
  });

  it("renders channel messages newest at the bottom with direction, status, and errors", async () => {
    setupFetch([
      [
        message({
          id: "msg-new",
          direction: "outbound",
          content: "Deploy finished",
          status: "delivered",
          created_at: "2026-01-02T00:00:00Z",
        }),
        message({
          id: "msg-old",
          direction: "inbound",
          content: "Ship it",
          status: "failed",
          error: "Slack rejected the payload",
          created_at: "2026-01-01T00:00:00Z",
        }),
      ],
    ]);
    const user = userEvent.setup();
    const onClose = vi.fn();

    render(<MessagesView channel={channel} onClose={onClose} />);

    expect(
      await screen.findByRole("heading", { name: "Messages" }),
    ).toBeInTheDocument();
    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Ship it")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Inbound")).toBeInTheDocument();
    expect(within(rows[0]).getByText("failed")).toBeInTheDocument();
    expect(
      within(rows[0]).getByText("Slack rejected the payload"),
    ).toBeInTheDocument();
    expect(within(rows[1]).getByText("Deploy finished")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Outbound")).toBeInTheDocument();
    expect(within(rows[1]).getByText("delivered")).toBeInTheDocument();
    expect(requestUrls()[0]).toContain("channel_id=ch-slack");
    expect(requestUrls()[0]).toContain("limit=20");

    await user.click(screen.getByRole("button", { name: "Close messages" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("loads older messages by increasing the limit", async () => {
    setupFetch([
      [
        message({
          id: "msg-new",
          direction: "outbound",
          content: "Current page",
          created_at: "2026-01-02T00:00:00Z",
        }),
      ],
      [
        message({
          id: "msg-old",
          direction: "inbound",
          content: "Earlier page",
          created_at: "2026-01-01T00:00:00Z",
        }),
        message({
          id: "msg-new",
          direction: "outbound",
          content: "Current page",
          created_at: "2026-01-02T00:00:00Z",
        }),
      ],
    ]);
    const user = userEvent.setup();

    render(<MessagesView channel={channel} onClose={vi.fn()} />);

    expect(await screen.findByText("Current page")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Load older messages" }),
    );

    await waitFor(() => {
      const urls = requestUrls();
      expect(urls[urls.length - 1]).toContain("limit=40");
    });
    expect(await screen.findByText("Earlier page")).toBeInTheDocument();
  });

  it("renders a designed empty state for channels without messages", async () => {
    setupFetch([[]]);

    render(<MessagesView channel={channel} onClose={vi.fn()} />);

    expect(await screen.findByText("No messages yet")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Recent inbound and outbound messages for Release alerts will appear here.",
      ),
    ).toBeInTheDocument();
  });
});
