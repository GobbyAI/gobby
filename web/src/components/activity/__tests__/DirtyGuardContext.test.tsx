import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DirtyGuardProvider } from "../DirtyGuardContext";
import { useDirtyGuardController } from "../dirtyGuard";
import { useDetailDraft } from "../fields";
import { useActivityPanel } from "../useActivityPanel";

interface DraftSource {
  id: string;
  name: string;
}

const source: DraftSource = {
  id: "draft-1",
  name: "Original",
};

afterEach(() => {
  vi.restoreAllMocks();
});

function DraftPane() {
  const draft = useDetailDraft({
    source,
    onSave: async () => true,
  });

  return (
    <label>
      Name
      <input
        aria-label="Name"
        value={draft.draft?.name ?? ""}
        onChange={(event) => draft.setField("name", event.target.value)}
      />
    </label>
  );
}

function DirtyGuardHarness({ startWithPane = true }: { startWithPane?: boolean }) {
  const dirtyGuard = useDirtyGuardController();
  const [tab, setTab] = useState("sessions");
  const [showPane, setShowPane] = useState(startWithPane);

  return (
    <DirtyGuardProvider value={dirtyGuard}>
      <div data-testid="active-tab">{tab}</div>
      <button type="button" onClick={() => dirtyGuard.guardedRun(() => setTab("tasks"))}>
        Tasks
      </button>
      <button type="button" onClick={() => setShowPane(false)}>
        Unmount pane
      </button>
      {showPane ? <DraftPane /> : null}
    </DirtyGuardProvider>
  );
}

describe("DirtyGuardContext (#17018)", () => {
  it("lets non-dirty panes pass through guarded shell actions", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");

    render(<DirtyGuardHarness />);

    await userEvent.click(screen.getByRole("button", { name: "Tasks" }));

    expect(screen.getByTestId("active-tab")).toHaveTextContent("tasks");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("blocks a dirty pane tab change when discard is declined", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValueOnce(false);

    render(<DirtyGuardHarness />);

    await userEvent.clear(screen.getByRole("textbox", { name: "Name" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Draft");
    await userEvent.click(screen.getByRole("button", { name: "Tasks" }));

    expect(confirmSpy).toHaveBeenCalledWith("Discard unsaved changes?");
    expect(screen.getByTestId("active-tab")).toHaveTextContent("sessions");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Draft");
  });

  it("discards and proceeds when the dirty pane confirm is accepted", async () => {
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);

    render(<DirtyGuardHarness />);

    await userEvent.clear(screen.getByRole("textbox", { name: "Name" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Draft");
    await userEvent.click(screen.getByRole("button", { name: "Tasks" }));

    expect(screen.getByTestId("active-tab")).toHaveTextContent("tasks");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Original");
  });

  it("unregisters draft guards when the pane unmounts", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");

    render(<DirtyGuardHarness />);

    await userEvent.clear(screen.getByRole("textbox", { name: "Name" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Draft");
    await userEvent.click(screen.getByRole("button", { name: "Unmount pane" }));
    await userEvent.click(screen.getByRole("button", { name: "Tasks" }));

    expect(screen.getByTestId("active-tab")).toHaveTextContent("tasks");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("routes activity-tab and desktop layout transitions through the registry", async () => {
    localStorage.clear();
    const { result } = renderHook(() => useActivityPanel(false));
    const confirmLeave = vi.fn(async () => false);

    act(() => {
      result.current.dirtyGuard.registerDirtyGuard({
        isDirty: () => true,
        confirmLeave,
      });
    });

    act(() => {
      result.current.setActiveTab("tasks");
    });

    await waitFor(() => expect(confirmLeave).toHaveBeenCalledTimes(1));
    expect(result.current.activeTab).toBe("sessions");

    act(() => {
      result.current.toggleFromPanel();
    });

    await waitFor(() => expect(confirmLeave).toHaveBeenCalledTimes(2));
    expect(result.current.effectiveMode).toBe("split");
  });

  it("routes mobile panel close through the registry", async () => {
    localStorage.clear();
    const { result } = renderHook(() => useActivityPanel(true));
    const confirmLeave = vi.fn(async () => false);

    act(() => {
      result.current.toggleFromChat();
    });
    expect(result.current.effectiveMode).toBe("panel");

    act(() => {
      result.current.dirtyGuard.registerDirtyGuard({
        isDirty: () => true,
        confirmLeave,
      });
    });

    act(() => {
      result.current.toggleFromPanel();
    });

    await waitFor(() => expect(confirmLeave).toHaveBeenCalledTimes(1));
    expect(result.current.effectiveMode).toBe("panel");
  });
});
