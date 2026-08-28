import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GobbySession } from "../../../types/sessions";
import { ResumeSessionModal } from "../ResumeSessionModal";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function session(id: string, title: string): GobbySession {
  return {
    id,
    ref: `#${id}`,
    external_id: id,
    source: "claude",
    project_id: "project-1",
    title,
    status: "active",
    model: null,
    message_count: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    seq_num: null,
    summary_markdown: null,
    handoff_markdown: null,
    git_branch: null,
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: null,
  };
}

function responseWith(sessions: GobbySession[]): Response {
  return {
    ok: true,
    json: vi.fn().mockResolvedValue({ sessions }),
  } as unknown as Response;
}

describe("ResumeSessionModal", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("names the dialog with its visible title", async () => {
    fetchMock.mockResolvedValue(responseWith([]));

    render(
      <ResumeSessionModal
        isOpen
        onClose={vi.fn()}
        sessions={[]}
        onResume={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("dialog", { name: "Resume Session" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("No resumable sessions"),
    ).toBeInTheDocument();
  });

  it("aborts the previous request and ignores its stale response", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    fetchMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    render(
      <ResumeSessionModal
        isOpen
        onClose={vi.fn()}
        sessions={[]}
        onResume={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: "Subagents" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    const firstSignal = fetchMock.mock.calls[0][1]?.signal;
    expect(firstSignal?.aborted).toBe(true);

    await act(async () => {
      second.resolve(responseWith([session("new", "Newest response")]));
    });
    expect(await screen.findByText("Newest response")).toBeInTheDocument();

    await act(async () => {
      first.resolve(responseWith([session("old", "Stale response")]));
    });
    expect(screen.queryByText("Stale response")).not.toBeInTheDocument();
    expect(screen.getByText("Newest response")).toBeInTheDocument();
  });

  it("uses the latest sessions fallback without refetching on prop identity changes", async () => {
    const request = deferred<Response>();
    fetchMock.mockReturnValue(request.promise);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    const { rerender } = render(
      <ResumeSessionModal
        isOpen
        onClose={vi.fn()}
        sessions={[session("initial", "Initial fallback")]}
        onResume={vi.fn()}
      />,
    );

    rerender(
      <ResumeSessionModal
        isOpen
        onClose={vi.fn()}
        sessions={[session("latest", "Latest fallback")]}
        onResume={vi.fn()}
      />,
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      request.resolve({
        ok: false,
        status: 500,
        statusText: "Server Error",
      } as Response);
    });

    expect(await screen.findByText("Latest fallback")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
