import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DetailPaneHeader, useDetailDraft } from "../";

interface RuleDraft {
  id: string;
  name: string;
  enabled: boolean;
  description: string;
}

const source: RuleDraft = {
  id: "rule-1",
  name: "Original",
  enabled: true,
  description: "Current description",
};

describe("useDetailDraft (#17014)", () => {
  it("tracks edits as dirty and discard restores the source", () => {
    const onSave = vi.fn(async () => true);
    const { result } = renderHook(() => useDetailDraft({ source, onSave }));

    act(() => {
      result.current.setField("name", "Draft");
    });

    expect(result.current.draft?.name).toBe("Draft");
    expect(result.current.dirty).toBe(true);
    expect(result.current.serverChanged).toBe(false);

    act(() => {
      result.current.discard();
    });

    expect(result.current.draft).toEqual(source);
    expect(result.current.dirty).toBe(false);
  });

  it("keeps a dirty draft and marks serverChanged when the source updates", () => {
    const onSave = vi.fn(async () => true);
    const { result, rerender } = renderHook(
      ({ nextSource }: { nextSource: RuleDraft }) =>
        useDetailDraft({ source: nextSource, onSave }),
      { initialProps: { nextSource: source } },
    );

    act(() => {
      result.current.setField("name", "Draft");
    });

    const serverUpdated = { ...source, description: "Changed on server" };
    rerender({ nextSource: serverUpdated });

    expect(result.current.draft?.name).toBe("Draft");
    expect(result.current.draft?.description).toBe("Current description");
    expect(result.current.serverChanged).toBe(true);
  });

  it("ignores a re-fetched source with identical values while dirty", () => {
    const onSave = vi.fn(async () => true);
    const { result, rerender } = renderHook(
      ({ nextSource }: { nextSource: RuleDraft }) =>
        useDetailDraft({ source: nextSource, onSave }),
      { initialProps: { nextSource: source } },
    );

    act(() => {
      result.current.setField("name", "Draft");
    });

    // Same values, new object identity — e.g. a post-save refetch or an
    // unrelated key changing elsewhere in a shared config snapshot.
    rerender({ nextSource: { ...source } });

    expect(result.current.serverChanged).toBe(false);
    expect(result.current.draft?.name).toBe("Draft");
  });

  it("saves edited keys over the latest source", async () => {
    const onSave = vi.fn(async () => true);
    const { result, rerender } = renderHook(
      ({ nextSource }: { nextSource: RuleDraft }) =>
        useDetailDraft({ source: nextSource, onSave }),
      { initialProps: { nextSource: source } },
    );

    act(() => {
      result.current.setField("name", "Draft");
    });
    rerender({
      nextSource: {
        ...source,
        enabled: false,
        description: "Fresh from server",
      },
    });

    await act(async () => {
      await result.current.save();
    });

    expect(onSave).toHaveBeenCalledWith({
      ...source,
      name: "Draft",
      enabled: false,
      description: "Fresh from server",
    });
    expect(result.current.dirty).toBe(false);
    expect(result.current.serverChanged).toBe(false);
  });

  it("guards dirty transitions through one confirm path", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    const next = vi.fn();
    const onSave = vi.fn(async () => true);
    const { result } = renderHook(() => useDetailDraft({ source, onSave }));

    act(() => {
      result.current.setField("name", "Draft");
    });

    act(() => {
      result.current.confirmIfDirty(next);
    });

    expect(confirmSpy).toHaveBeenCalled();
    expect(next).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });
});

describe("DetailPaneHeader (#17014)", () => {
  it("renders save and discard only for dirty drafts", () => {
    const { rerender } = render(
      <DetailPaneHeader title="Rule detail" dirty={false} onSave={vi.fn()} onDiscard={vi.fn()} />,
    );

    expect(screen.getByText("Rule detail")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();

    rerender(
      <DetailPaneHeader title="Rule detail" dirty onSave={vi.fn()} onDiscard={vi.fn()} />,
    );

    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
  });

  it("shows server-change notice and action slots in the status strip", () => {
    const onSave = vi.fn();
    const onDiscard = vi.fn();

    render(
      <DetailPaneHeader
        title={<span>Rule detail</span>}
        dirty
        saving={false}
        serverChanged
        actions={<button type="button">Reload</button>}
        onSave={onSave}
        onDiscard={onDiscard}
      />,
    );

    expect(screen.getByText("Changed on server")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(onSave).toHaveBeenCalled();
    expect(onDiscard).toHaveBeenCalled();
  });
});
