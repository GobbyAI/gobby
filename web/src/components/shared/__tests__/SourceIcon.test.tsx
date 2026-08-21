import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { SourceIcon } from "../SourceIcon";
import {
  PROVIDER_COLOR_PAIRS,
  SOURCE_COLOR_PAIRS,
  SOURCE_LABELS,
} from "../sourceTheme";
import { KNOWN_SOURCES } from "../../../types/sessions";

describe("SourceIcon", () => {
  it("renders the Codex icon as an inline svg", () => {
    const { container } = render(<SourceIcon source="codex" size={16} />);

    const icon = container.querySelector("svg.source-icon-codex");
    expect(icon).toBeTruthy();
    expect(container.querySelector("img.source-icon-codex")).toBeNull();
  });

  it("renders the Grok icon as an inline svg", () => {
    const { container } = render(<SourceIcon source="grok" size={16} />);

    const icon = container.querySelector("svg.source-icon-grok");
    expect(icon).toBeTruthy();
    expect(icon?.querySelector("path")).toBeTruthy();
    expect(container.querySelector("img.source-icon-grok")).toBeNull();
    expect(container.querySelector("circle")).toBeNull();
  });

  it("renders the Ollama icon as an inline svg, not the fallback circle", () => {
    const { container } = render(<SourceIcon source="ollama" size={16} />);

    const icon = container.querySelector("svg.source-icon-ollama");
    expect(icon).toBeTruthy();
    expect(icon?.querySelector("path")).toBeTruthy();
    expect(icon?.getAttribute("fill")).toBe("currentColor");
    expect(container.querySelector("circle")).toBeNull();
  });

  it("renders the LM Studio icon as an inline svg, not the fallback circle", () => {
    const { container } = render(<SourceIcon source="lmstudio" size={16} />);

    const icon = container.querySelector("svg.source-icon-lmstudio");
    expect(icon).toBeTruthy();
    expect(icon?.querySelector("path")).toBeTruthy();
    expect(icon?.getAttribute("fill")).toBe("currentColor");
    expect(container.querySelector("circle")).toBeNull();
  });

  it("renders the vLLM icon as an inline svg, not the fallback circle", () => {
    const { container } = render(<SourceIcon source="vllm" size={16} />);

    const icon = container.querySelector("svg.source-icon-vllm");
    expect(icon).toBeTruthy();
    expect(icon?.querySelector("path")).toBeTruthy();
    expect(icon?.getAttribute("fill")).toBe("currentColor");
    expect(container.querySelector("circle")).toBeNull();
    expect(container.querySelector("img.source-icon-vllm")).toBeNull();
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

  it("renders the Droid provider as an image", () => {
    const { container } = render(<SourceIcon source="droid" size={16} />);

    const icon = container.querySelector("img.source-icon-droid");
    expect(icon).toBeTruthy();
  });

  it("includes Droid in shared source metadata", () => {
    expect(KNOWN_SOURCES).toContain("droid");
    expect(SOURCE_LABELS.droid).toBe("Droid");
    expect(SOURCE_COLOR_PAIRS.droid).toBeTruthy();
    expect(PROVIDER_COLOR_PAIRS.droid.dark).toBe(SOURCE_COLOR_PAIRS.droid.dark);
    expect(PROVIDER_COLOR_PAIRS.droid.light).toBe(
      SOURCE_COLOR_PAIRS.droid.light,
    );
  });

  it("renders unknown sources with the neutral fallback glyph", () => {
    expect(KNOWN_SOURCES).toContain("unknown");
    expect(SOURCE_LABELS.unknown).toBe("Unknown");
    expect(SOURCE_COLOR_PAIRS.unknown).toBeTruthy();

    const { container } = render(<SourceIcon source="unknown" size={16} />);

    expect(container.querySelector("svg.source-icon")).toBeTruthy();
    expect(container.querySelector("circle")).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
  });
});
