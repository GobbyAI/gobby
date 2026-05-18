import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  TaskSelectField,
  TaskTagsField,
  TaskTextAreaField,
  TaskTextField,
} from "../TaskFieldEditors";

describe("TaskTextField (#14771 / D4)", () => {
  it("commits the trimmed value on Enter", () => {
    const onCommit = vi.fn();
    render(
      <TaskTextField value="Old" onCommit={onCommit} ariaLabel="Title" />,
    );
    const input = screen.getByLabelText("Title") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  New  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenCalledWith("New");
    expect(input.value).toBe("New");
  });

  it("Escape reverts the draft and does not commit", () => {
    const onCommit = vi.fn();
    render(
      <TaskTextField value="Keep" onCommit={onCommit} ariaLabel="Title" />,
    );
    const input = screen.getByLabelText("Title") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "discarded" } });
    fireEvent.keyDown(input, { key: "Escape" });
    fireEvent.blur(input);
    expect(input.value).toBe("Keep");
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("does not commit when the value is unchanged", () => {
    const onCommit = vi.fn();
    render(
      <TaskTextField value="Same" onCommit={onCommit} ariaLabel="Title" />,
    );
    fireEvent.blur(screen.getByLabelText("Title"));
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("trims an unchanged draft without committing", () => {
    const onCommit = vi.fn();
    render(
      <TaskTextField value="Same" onCommit={onCommit} ariaLabel="Title" />,
    );
    const input = screen.getByLabelText("Title") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  Same  " } });
    fireEvent.blur(input);
    expect(input.value).toBe("Same");
    expect(onCommit).not.toHaveBeenCalled();
  });
});

describe("TaskTextAreaField (#14771 / D4)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("debounce-commits after inactivity", () => {
    vi.useFakeTimers();
    const onCommit = vi.fn();
    render(
      <TaskTextAreaField
        value=""
        onCommit={onCommit}
        ariaLabel="Description"
        debounceMs={500}
      />,
    );
    const area = screen.getByLabelText("Description") as HTMLTextAreaElement;
    fireEvent.change(area, {
      target: { value: "  draft body  " },
    });
    expect(onCommit).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(onCommit).toHaveBeenCalledWith("draft body");
    expect(area.value).toBe("draft body");
  });

  it("Escape reverts and cancels the pending debounce", () => {
    vi.useFakeTimers();
    const onCommit = vi.fn();
    render(
      <TaskTextAreaField
        value="kept"
        onCommit={onCommit}
        ariaLabel="Description"
        debounceMs={500}
      />,
    );
    const area = screen.getByLabelText("Description") as HTMLTextAreaElement;
    fireEvent.change(area, { target: { value: "throwaway" } });
    fireEvent.keyDown(area, { key: "Escape" });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(area.value).toBe("kept");
    expect(onCommit).not.toHaveBeenCalled();
  });
});

describe("TaskSelectField (#14771 / D4)", () => {
  it("commits the chosen option", () => {
    const onCommit = vi.fn();
    render(
      <TaskSelectField
        value="task"
        ariaLabel="Type"
        onCommit={onCommit}
        options={[
          { value: "task", label: "Task" },
          { value: "bug", label: "Bug" },
        ]}
      />,
    );
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "bug" },
    });
    expect(onCommit).toHaveBeenCalledWith("bug");
  });
});

describe("TaskTagsField (#14771 / D4)", () => {
  it("adds a tag on Enter and commits the set immediately", () => {
    const onCommit = vi.fn();
    render(
      <TaskTagsField value={["web"]} onCommit={onCommit} ariaLabel="Labels" />,
    );
    const input = screen.getByLabelText("Add label");
    fireEvent.change(input, { target: { value: "ui" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCommit).toHaveBeenCalledWith(["web", "ui"]);
  });

  it("removes the last tag on Backspace when the entry is empty", () => {
    const onCommit = vi.fn();
    render(
      <TaskTagsField
        value={["a", "b"]}
        onCommit={onCommit}
        ariaLabel="Labels"
      />,
    );
    const input = screen.getByLabelText("Add label");
    fireEvent.keyDown(input, { key: "Backspace" });
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenCalledWith(["a"]);
  });

  it("Escape reverts the tag set without committing", () => {
    const onCommit = vi.fn();
    render(
      <TaskTagsField value={["keep"]} onCommit={onCommit} ariaLabel="Labels" />,
    );
    const input = screen.getByLabelText("Add label");
    fireEvent.change(input, { target: { value: "extra" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onCommit).not.toHaveBeenCalled();
  });
});
