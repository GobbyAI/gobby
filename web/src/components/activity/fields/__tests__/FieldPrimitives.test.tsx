import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  KeyValueField,
  NumberField,
  SelectField,
  SwitchField,
  TagsField,
  TextAreaField,
  TextField,
} from "../";

describe("draft field primitives (#17014)", () => {
  it("renders controlled text inputs without committing on blur", () => {
    const onChange = vi.fn();

    render(
      <TextField
        label="Rule name"
        value="Existing"
        onChange={onChange}
        ariaLabel="Rule name"
      />,
    );

    const input = screen.getByLabelText("Rule name");
    fireEvent.change(input, { target: { value: "Draft" } });
    fireEvent.blur(input);

    expect(onChange).toHaveBeenCalledWith("Draft");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("renders textarea and select controls as controlled draft fields", () => {
    const onTextAreaChange = vi.fn();
    const onSelectChange = vi.fn();

    render(
      <>
        <TextAreaField
          label="Description"
          value="Body"
          onChange={onTextAreaChange}
          ariaLabel="Description"
        />
        <SelectField
          label="Mode"
          value="live"
          onChange={onSelectChange}
          ariaLabel="Mode"
          options={[
            { value: "live", label: "Live" },
            { value: "archived", label: "Archived" },
          ]}
        />
      </>,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Next body" },
    });
    fireEvent.change(screen.getByLabelText("Mode"), {
      target: { value: "archived" },
    });

    expect(onTextAreaChange).toHaveBeenCalledWith("Next body");
    expect(onSelectChange).toHaveBeenCalledWith("archived");
  });

  it("adds and removes tags through a controlled string-array chip field", () => {
    const onChange = vi.fn();

    render(
      <TagsField
        label="Labels"
        value={["web"]}
        onChange={onChange}
        ariaLabel="Labels"
      />,
    );

    // Compact chip-box canon (#19151): the add input stays at the dense
    // control height instead of promoting the whole box to the 44px floor.
    const addInput = screen.getByLabelText("Add Labels");
    expect(addInput).toHaveClass("min-h-7");
    expect(addInput).not.toHaveClass("min-h-11");

    fireEvent.change(addInput, {
      target: { value: "ui" },
    });
    fireEvent.keyDown(addInput, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith(["web", "ui"]);

    fireEvent.click(screen.getByLabelText("Remove web"));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("wraps the shared Switch control with a visible label", () => {
    const onChange = vi.fn();

    render(
      <SwitchField
        label="Enabled"
        value={false}
        onChange={onChange}
        ariaLabel="Enabled"
      />,
    );

    const toggle = screen.getByRole("switch", { name: "Enabled" });
    fireEvent.click(toggle);

    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("edits key/value records by row", () => {
    const onChange = vi.fn();

    render(
      <KeyValueField
        label="Environment"
        value={{ API_KEY: "old" }}
        onChange={onChange}
        ariaLabel="Environment"
      />,
    );

    const group = screen.getByRole("group", { name: "Environment" });
    fireEvent.change(within(group).getByLabelText("Key 1"), {
      target: { value: "TOKEN" },
    });
    expect(onChange).toHaveBeenCalledWith({ TOKEN: "old" });

    fireEvent.change(within(group).getByLabelText("Value 1"), {
      target: { value: "new" },
    });
    expect(onChange).toHaveBeenCalledWith({ API_KEY: "new" });

    fireEvent.click(within(group).getByRole("button", { name: "Add row" }));
    expect(onChange).toHaveBeenCalledWith({ API_KEY: "old", "": "" });

    fireEvent.click(within(group).getByRole("button", { name: "Remove API_KEY" }));
    expect(onChange).toHaveBeenCalledWith({});
  });

  it("shows visible feedback when a non-empty key duplicates another row", () => {
    const onChange = vi.fn();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    try {
      render(
        <KeyValueField
          label="Environment"
          value={{ API_KEY: "old", TOKEN: "new" }}
          onChange={onChange}
          ariaLabel="Environment"
        />,
      );

      const group = screen.getByRole("group", { name: "Environment" });
      fireEvent.change(within(group).getByLabelText("Key 2"), {
        target: { value: "API_KEY" },
      });

      expect(onChange).not.toHaveBeenCalled();
      expect(screen.getByRole("alert")).toHaveTextContent(
        'Key "API_KEY" already exists',
      );
      expect(warn).toHaveBeenCalledWith(
        'KeyValueField: ignored rename to duplicate key "API_KEY" to avoid overwriting an existing entry',
      );
    } finally {
      warn.mockRestore();
    }
  });

  it("allows empty draft keys to be edited without duplicate feedback", () => {
    const onChange = vi.fn();

    render(
      <KeyValueField
        label="Environment"
        value={{ "": "draft", API_KEY: "old" }}
        onChange={onChange}
        ariaLabel="Environment"
      />,
    );

    const group = screen.getByRole("group", { name: "Environment" });
    fireEvent.change(within(group).getByLabelText("Key 2"), {
      target: { value: "" },
    });

    expect(onChange).toHaveBeenCalledWith({ "": "old" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders a numeric input that emits parsed numbers and null when cleared", () => {
    const onChange = vi.fn();

    render(
      <NumberField
        label="Timeout"
        value={600}
        onChange={onChange}
        ariaLabel="Timeout"
        min={1}
        max={3600}
      />,
    );

    const input = screen.getByLabelText("Timeout");
    expect(input).toHaveAttribute("type", "number");
    expect(input).toHaveAttribute("min", "1");
    expect(input).toHaveAttribute("max", "3600");
    expect(input).toHaveClass("min-h-11");
    expect(input).toHaveValue(600);

    fireEvent.change(input, { target: { value: "120" } });
    expect(onChange).toHaveBeenCalledWith(120);

    fireEvent.change(input, { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(null);
  });
});
