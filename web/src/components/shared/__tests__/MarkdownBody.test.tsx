import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownBody, markdownBodyClassName } from "../MarkdownBody";

describe("MarkdownBody", () => {
  it("gives unordered and ordered lists their markers", () => {
    const { container } = render(
      <div className={markdownBodyClassName}>
        <MarkdownBody
          id="lists"
          content={"- first\n- second\n\n1. one\n2. two\n"}
        />
      </div>,
    );

    const host = container.firstElementChild;
    if (!(host instanceof HTMLElement)) throw new Error("host not rendered");
    expect(host.querySelector("ul")).not.toBeNull();
    expect(host.querySelector("ol")).not.toBeNull();
    // Tailwind preflight strips list-style, so the host must restore markers.
    expect(host.classList).toContain("[&_ul]:list-disc");
    expect(host.classList).toContain("[&_ol]:list-decimal");
  });

  it("keeps task-list items free of markers", () => {
    expect(markdownBodyClassName.split(" ")).toContain(
      "[&_li:has(>input[type=checkbox])]:list-none",
    );
  });
});
