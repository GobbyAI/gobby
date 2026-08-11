/**
 * The dormancy strip renders only while the daemon reports the codewiki
 * surface disabled, or when its status is unreachable. A live surface gets
 * no strip at all — no placeholder badge, no raw reason token.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WikiCodewikiStatus } from "../WikiCodewikiStatus";
import { fetchCodewikiStatus } from "../WikiTabData";

vi.mock("../WikiTabData", () => ({
  fetchCodewikiStatus: vi.fn(),
}));

const mockFetchCodewikiStatus = vi.mocked(fetchCodewikiStatus);

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("WikiCodewikiStatus", () => {
  it("renders the paused strip while the surface is disabled", async () => {
    mockFetchCodewikiStatus.mockResolvedValue({
      enabled: false,
      state: "disabled",
      reason: "pending_wiki_redesign",
    });

    render(<WikiCodewikiStatus />);

    expect(await screen.findByText("Paused")).toBeTruthy();
    expect(screen.getByText("Generation paused pending wiki redesign.")).toBeTruthy();
  });

  it("renders nothing once the surface is live", async () => {
    mockFetchCodewikiStatus.mockResolvedValue({
      enabled: true,
      state: "enabled",
      reason: "",
    });

    const { container } = render(<WikiCodewikiStatus />);

    await waitFor(() => expect(mockFetchCodewikiStatus).toHaveBeenCalled());
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it("renders the unavailable strip when status cannot be fetched", async () => {
    mockFetchCodewikiStatus.mockRejectedValue(new Error("status route down"));

    render(<WikiCodewikiStatus />);

    expect(await screen.findByText("Unavailable")).toBeTruthy();
    expect(screen.getByText("Codewiki status unavailable")).toBeTruthy();
  });

  it("keeps polling after the initial status request fails", async () => {
    vi.useFakeTimers();
    mockFetchCodewikiStatus
      .mockRejectedValueOnce(new Error("status route starting"))
      .mockResolvedValue({ enabled: true, state: "enabled", reason: "" });

    const { container } = render(<WikiCodewikiStatus />);
    await act(async () => Promise.resolve());
    expect(screen.getByText("Unavailable")).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(30_000));

    expect(mockFetchCodewikiStatus).toHaveBeenCalledTimes(2);
    expect(container.firstChild).toBeNull();
  });

  it("stops polling when a later snapshot disables the surface", async () => {
    vi.useFakeTimers();
    mockFetchCodewikiStatus
      .mockResolvedValueOnce({ enabled: true, state: "enabled", reason: "" })
      .mockResolvedValue({
        enabled: false,
        state: "disabled",
        reason: "pending_wiki_redesign",
      });

    render(<WikiCodewikiStatus />);
    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTimeAsync(30_000));

    expect(screen.getByText("Paused")).toBeTruthy();
    expect(mockFetchCodewikiStatus).toHaveBeenCalledTimes(2);

    await act(async () => vi.advanceTimersByTimeAsync(90_000));
    expect(mockFetchCodewikiStatus).toHaveBeenCalledTimes(2);
  });
});
