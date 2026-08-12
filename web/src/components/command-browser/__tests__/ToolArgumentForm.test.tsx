import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ToolArgumentForm } from "../ToolArgumentForm";

const objectSchema = {
  properties: {
    payload: { type: "object" },
  },
};

function FormHarness({
  onValidityChange,
}: {
  onValidityChange?: (valid: boolean) => void;
}) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  return (
    <ToolArgumentForm
      schema={objectSchema}
      values={values}
      onChange={setValues}
      onValidityChange={onValidityChange}
    />
  );
}

describe("ToolArgumentForm JSON fields", () => {
  it("preserves raw valid JSON while typing and formats it on blur", () => {
    render(<FormHarness />);
    const textarea = screen.getByRole("textbox", { name: "payload" });

    fireEvent.change(textarea, { target: { value: '{"nested":{"value":1}}' } });

    expect(textarea).toHaveValue('{"nested":{"value":1}}');
    fireEvent.blur(textarea);
    expect(textarea).toHaveValue('{\n  "nested": {\n    "value": 1\n  }\n}');
  });

  it("shows an inline error and reports invalid JSON until every field parses", () => {
    const onValidityChange = vi.fn();
    const schema = {
      properties: {
        payload: { type: "object" },
        items: { type: "array" },
      },
    };
    render(
      <ToolArgumentForm
        schema={schema}
        values={{}}
        onChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );

    const payload = screen.getByRole("textbox", { name: "payload" });
    const items = screen.getByRole("textbox", { name: "items" });
    fireEvent.change(payload, { target: { value: "{" } });
    fireEvent.change(items, { target: { value: "[" } });

    expect(screen.getAllByRole("alert")).toHaveLength(2);
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(payload, { target: { value: "{}" } });
    expect(onValidityChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(items, { target: { value: "[]" } });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });
});
