import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatInput } from "../ChatInput";
import { WikiActionResult } from "../WikiActionResult";

const defaultProps = {
  onSend: vi.fn(),
};

function jsonResponse(payload: unknown, init?: ResponseInit) {
  return Response.json(payload, init);
}

describe("wiki chat actions", () => {
  const fetchMock = vi.fn();
  const onWikiActionComplete = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    onWikiActionComplete.mockReset();
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/wiki/status")) return jsonResponse({ ok: true, payload: {} });
      if (url.includes("/api/wiki/health")) {
        return jsonResponse({ ok: true, payload: { status: "healthy" } });
      }
      if (url.includes("/api/wiki/sources")) return jsonResponse({ ok: true, payload: {} });
      if (url.includes("/api/wiki/search")) {
        return jsonResponse({
          ok: true,
          payload: {
            query: "hooks",
            citations: [{ title: "Hooks", path: "wiki/hooks.md", source_path: "raw/hooks.md" }],
          },
        });
      }
      if (url.includes("/api/wiki/ingest") && init?.method === "POST") {
        return jsonResponse({
          ok: true,
          payload: {
            status: "partial",
            accepted: [{ requested_url: "https://example.test/a", raw_path: "raw/a.md" }],
            failed: [{ url: "https://example.test/b", message: "blocked" }],
          },
        });
      }
      if (url.includes("/api/wiki/compile") && init?.method === "POST") {
        return jsonResponse({ ok: true, payload: { changed_paths: ["wiki/index.md"] } });
      }
      return jsonResponse({ ok: true, payload: {} });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("test_actions_mounted_in_chat_input", () => {
    render(<ChatInput {...defaultProps} projectId="demo" />);

    expect(screen.getByRole("button", { name: "Wiki actions" })).toBeInTheDocument();
  });

  it("test_writes_require_explicit_intent", async () => {
    const user = userEvent.setup();
    render(<ChatInput {...defaultProps} projectId="demo" />);

    await user.click(screen.getByRole("button", { name: "Wiki actions" }));
    await user.click(screen.getByRole("button", { name: "Compile wiki" }));

    expect(screen.getByText("Confirm wiki write")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/wiki/compile"),
      expect.anything(),
    );

    await user.click(screen.getByRole("button", { name: "Run wiki write" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/wiki/compile?project=demo"),
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("test_url_batch_ingest_action", async () => {
    const user = userEvent.setup();
    render(<ChatInput {...defaultProps} projectId="demo" />);

    await user.click(screen.getByRole("button", { name: "Wiki actions" }));
    await user.click(screen.getByRole("button", { name: "Ingest URLs" }));
    await user.type(
      screen.getByLabelText("Wiki action input"),
      "https://example.test/a\nhttps://example.test/b",
    );
    await user.click(screen.getByRole("button", { name: "Run wiki write" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/wiki/ingest?project=demo"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            urls: ["https://example.test/a", "https://example.test/b"],
          }),
        }),
      );
    });
    expect(await screen.findByText("Accepted")).toBeInTheDocument();
    expect(screen.getAllByText("https://example.test/a").length).toBeGreaterThan(0);
    expect(screen.getAllByText("raw/a.md").length).toBeGreaterThan(0);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getAllByText("https://example.test/b").length).toBeGreaterThan(0);
    expect(screen.getByText("blocked")).toBeInTheDocument();
  });

  it("test_action_links_back_to_wiki_panel", async () => {
    const user = userEvent.setup();
    render(
      <ChatInput
        {...defaultProps}
        projectId="demo"
        onWikiActionComplete={onWikiActionComplete}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Wiki actions" }));
    await user.click(screen.getByRole("button", { name: "Search wiki" }));
    await user.type(screen.getByLabelText("Wiki action input"), "hooks");
    await user.click(screen.getByRole("button", { name: "Run wiki action" }));

    await waitFor(() => {
      expect(onWikiActionComplete).toHaveBeenCalled();
    });
  });

  it("renders citations, paths, sources, and degradation messages", () => {
    render(
      <WikiActionResult
        result={{
          kind: "health",
          title: "Wiki health",
          envelope: {
            ok: true,
            payload: {
              citations: [{ title: "Hooks", path: "wiki/hooks.md", source_path: "raw/hooks.md" }],
              wiki_path: "wiki/index.md",
              source_path: "raw/index.md",
              degraded_services: ["embeddings"],
            },
          },
        }}
      />,
    );

    expect(screen.getAllByText("Hooks").length).toBeGreaterThan(0);
    expect(screen.getAllByText("wiki/hooks.md").length).toBeGreaterThan(0);
    expect(screen.getAllByText("raw/hooks.md").length).toBeGreaterThan(0);
    expect(screen.getByText("wiki/index.md")).toBeInTheDocument();
    expect(screen.getByText("raw/index.md")).toBeInTheDocument();
    expect(within(screen.getByText("Degraded").closest("section")!).getByText("embeddings"))
      .toBeInTheDocument();
  });
});
