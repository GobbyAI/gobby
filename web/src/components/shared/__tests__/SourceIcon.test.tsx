import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { SourceIcon } from "../SourceIcon";

describe("SourceIcon", () => {
  it("renders the Codex icon as an inline svg", () => {
    const { container } = render(<SourceIcon source="codex" size={16} />);

    const icon = container.querySelector("svg.source-icon-codex");
    expect(icon).toBeTruthy();
    expect(container.querySelector("img.source-icon-codex")).toBeNull();
  });

  it("renders provider assets as images when available", () => {
    const { container } = render(<SourceIcon source="claude" size={16} />);

    const icon = container.querySelector("img.source-icon-claude");
    expect(icon).toBeTruthy();
  });

  it("renders the Qwen provider as an image", () => {
    const { container } = render(<SourceIcon source="qwen" size={16} />);

    const icon = container.querySelector("img.source-icon-qwen");
    expect(icon).toBeTruthy();
  });
});
