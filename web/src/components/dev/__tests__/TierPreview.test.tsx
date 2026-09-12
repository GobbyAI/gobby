import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { TierPreview } from "../TierPreview";
import {
  applyPointerOverride,
  isTierPreviewRequested,
} from "../tierPreviewConfig";

describe("isTierPreviewRequested", () => {
  it("detects the tier-preview query param", () => {
    expect(isTierPreviewRequested("?tier-preview")).toBe(true);
    expect(isTierPreviewRequested("?tier-preview=1&other=x")).toBe(true);
  });

  it("is off without the param", () => {
    expect(isTierPreviewRequested("")).toBe(false);
    expect(isTierPreviewRequested("?other=x")).toBe(false);
  });
});

describe("applyPointerOverride", () => {
  it("stamps the root only for ?pointer=coarse", () => {
    const root = document.createElement("html");
    applyPointerOverride("?other=x", root);
    expect(root.dataset.pointer).toBeUndefined();
    applyPointerOverride("?pointer=fine", root);
    expect(root.dataset.pointer).toBeUndefined();
    applyPointerOverride("?pointer=coarse", root);
    expect(root.dataset.pointer).toBe("coarse");
  });
});

describe("TierPreview", () => {
  it("defaults to the portrait tier with coarse-pointer emulation in the iframe src", () => {
    render(<TierPreview />);
    const frame = screen.getByTestId<HTMLIFrameElement>("tier-frame");
    expect(frame.getAttribute("src")).toBe("/?pointer=coarse");
    expect(frame.style.width).toBe("440px");
    expect(frame.style.height).toBe("956px");
    expect(screen.getByTestId("tier-size")).toHaveTextContent("440×956");
  });

  it("wraps fixed tiers in a scale wrapper sized at scale 1 without layout measurements", () => {
    render(<TierPreview />);
    const wrap = screen.getByTestId("tier-frame-wrap");
    expect(wrap.style.width).toBe("440px");
    expect(wrap.style.height).toBe("956px");
    const frame = screen.getByTestId<HTMLIFrameElement>("tier-frame");
    expect(frame.style.transform).toBe("scale(1)");
    expect(frame.style.transformOrigin).toBe("top left");
    expect(screen.getByTestId("tier-size")).not.toHaveTextContent("@");
  });

  it("switches to landscape dimensions", () => {
    render(<TierPreview />);
    fireEvent.click(screen.getByRole("radio", { name: "Landscape" }));
    const frame = screen.getByTestId<HTMLIFrameElement>("tier-frame");
    expect(frame.style.width).toBe("932px");
    expect(frame.style.height).toBe("430px");
    expect(screen.getByTestId("tier-size")).toHaveTextContent("932×430");
  });

  it("fills the stage on the desktop tier without a scale wrapper", () => {
    render(<TierPreview />);
    fireEvent.click(screen.getByRole("radio", { name: "Desktop" }));
    const frame = screen.getByTestId<HTMLIFrameElement>("tier-frame");
    expect(frame.className).toContain("h-full");
    expect(frame.className).toContain("w-full");
    // Desktop is a real fine-pointer tier: no emulation param.
    expect(frame.getAttribute("src")).toBe("/");
    expect(screen.queryByTestId("tier-frame-wrap")).toBeNull();
    expect(screen.getByTestId("tier-size")).toHaveTextContent("fill");
  });
});
