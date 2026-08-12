import { createElement, type ReactNode } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  Emphasis,
  Link,
  Paragraph,
  PhrasingContent,
  Root,
  Text,
} from "mdast";

import { MarkdownBody } from "../../../components/shared/MarkdownBody";
import { remarkWikilink, type RemarkWikilinkOptions } from "../remarkWikilink";

function text(value: string): Text {
  return { type: "text", value };
}

function paragraph(children: PhrasingContent[]): Paragraph {
  return { type: "paragraph", children };
}

function root(children: Paragraph[]): Root {
  return { type: "root", children };
}

function transform(tree: Root, options?: RemarkWikilinkOptions): Paragraph {
  remarkWikilink(options)(tree);
  return tree.children[0] as Paragraph;
}

function firstLink(para: Paragraph): Link {
  const link = para.children.find((child) => child.type === "link");
  if (!link) throw new Error("no link node produced");
  return link as Link;
}

describe("remarkWikilink transformer", () => {
  it("converts a bare wikilink into a wikilink-scheme link with surrounding text", () => {
    const para = transform(
      root([paragraph([text("see [[knowledge/concepts/gobby]] now")])]),
    );

    expect(para.children).toHaveLength(3);
    expect(para.children[0]).toEqual({ type: "text", value: "see " });
    expect(para.children[2]).toEqual({ type: "text", value: " now" });

    const link = firstLink(para);
    expect(link.url).toBe(
      `wikilink:${encodeURIComponent("knowledge/concepts/gobby")}`,
    );
    expect(link.children).toEqual([{ type: "text", value: "gobby" }]);
    expect(link.data?.hProperties).toMatchObject({
      className: "wikilink",
      "data-wiki-target": "knowledge/concepts/gobby",
    });
  });

  it("uses the alias as the link text when present", () => {
    const para = transform(
      root([
        paragraph([text("[[knowledge/sources/src-0001|Session: 019efb0c]]")]),
      ]),
    );

    const link = firstLink(para);
    expect(link.children).toEqual([
      { type: "text", value: "Session: 019efb0c" },
    ]);
    expect(link.data?.hProperties).toMatchObject({
      "data-wiki-target": "knowledge/sources/src-0001",
    });
  });

  it("keeps the anchor in the url and label but resolves only the page part", () => {
    const resolve = vi.fn(() => ({ path: "knowledge/concepts/gobby.md" }));
    const para = transform(
      root([paragraph([text("[[knowledge/concepts/gobby#History]]")])]),
      {
        resolve,
      },
    );

    expect(resolve).toHaveBeenCalledWith("knowledge/concepts/gobby");
    const link = firstLink(para);
    expect(link.url).toBe(
      `wikilink:${encodeURIComponent("knowledge/concepts/gobby#History")}`,
    );
    expect(link.children).toEqual([{ type: "text", value: "gobby#History" }]);
    expect(link.data?.hProperties).toMatchObject({ className: "wikilink" });
  });

  it("marks unresolved targets with the unresolved class and aria-description", () => {
    const para = transform(root([paragraph([text("[[missing/page]]")])]), {
      resolve: () => null,
    });

    const props = firstLink(para).data?.hProperties;
    expect(props).toMatchObject({
      className: "wikilink wikilink--unresolved",
      "data-wiki-target": "missing/page",
      "aria-description": "Page not created yet",
    });
  });

  it("renders resolved-optimistic without a resolver", () => {
    const para = transform(root([paragraph([text("[[missing/page]]")])]));

    const props = firstLink(para).data?.hProperties;
    expect(props).toMatchObject({ className: "wikilink" });
    expect(props).not.toHaveProperty("aria-description");
  });

  it("handles adjacent wikilinks without inserting empty text nodes", () => {
    const para = transform(root([paragraph([text("[[a]][[b]]")])]));

    expect(para.children).toHaveLength(2);
    expect(para.children.every((child) => child.type === "link")).toBe(true);
    expect((para.children[0] as Link).url).toBe("wikilink:a");
    expect((para.children[1] as Link).url).toBe("wikilink:b");
  });

  it("degrades embeds to plain links", () => {
    const para = transform(
      root([paragraph([text("![[knowledge/assets/diagram.png]]")])]),
    );

    expect(para.children).toHaveLength(1);
    const link = firstLink(para);
    expect(link.type).toBe("link");
    expect(link.url).toBe(
      `wikilink:${encodeURIComponent("knowledge/assets/diagram.png")}`,
    );
    expect(link.children).toEqual([{ type: "text", value: "diagram.png" }]);
  });

  it("strips a trailing .md from the derived label but not the target", () => {
    const para = transform(root([paragraph([text("[[knowledge/foo.md]]")])]));

    const link = firstLink(para);
    expect(link.children).toEqual([{ type: "text", value: "foo" }]);
    expect(link.data?.hProperties).toMatchObject({
      "data-wiki-target": "knowledge/foo.md",
    });
  });

  it("leaves text inside existing links untouched", () => {
    const inner: Link = {
      type: "link",
      url: "https://example.com",
      children: [text("see [[a]]")],
    };
    const tree = root([paragraph([inner])]);
    remarkWikilink()(tree);

    expect(inner.children).toEqual([{ type: "text", value: "see [[a]]" }]);
  });

  it("recurses into nested phrasing containers", () => {
    const emphasis: Emphasis = {
      type: "emphasis",
      children: [text("go [[b]]")],
    };
    transform(root([paragraph([emphasis])]));

    expect(emphasis.children).toHaveLength(2);
    expect(emphasis.children[0]).toEqual({ type: "text", value: "go " });
    expect((emphasis.children[1] as Link).url).toBe("wikilink:b");
  });

  it("leaves text without wikilinks unchanged", () => {
    const para = transform(root([paragraph([text("no links here")])]));

    expect(para.children).toEqual([{ type: "text", value: "no links here" }]);
  });
});

describe("MarkdownBody extension seam", () => {
  afterEach(cleanup);

  it("renders identically with an empty extension list and keeps default components", () => {
    const content = "**bold** and [link](https://example.com)";
    const { container: plain } = render(
      createElement(MarkdownBody, { content, id: "d1" }),
    );
    const { container: extended } = render(
      createElement(MarkdownBody, { content, id: "d1", remarkPlugins: [] }),
    );

    expect(extended.innerHTML).toBe(plain.innerHTML);
    const anchor = plain.querySelector("a");
    expect(anchor?.getAttribute("href")).toBe("https://example.com");
    expect(anchor?.className).toContain("text-accent");
  });

  it("renders wikilinks through the plugin seam", () => {
    const { container } = render(
      createElement(MarkdownBody, {
        content: "see [[knowledge/concepts/gobby|Gobby]]",
        id: "w1",
        remarkPlugins: [remarkWikilink],
      }),
    );

    const anchor = container.querySelector("a.wikilink");
    expect(anchor).not.toBeNull();
    expect(anchor?.getAttribute("href")).toBe(
      "wikilink:knowledge%2Fconcepts%2Fgobby",
    );
    expect(anchor?.getAttribute("data-wiki-target")).toBe(
      "knowledge/concepts/gobby",
    );
    expect(anchor?.textContent).toBe("Gobby");
  });

  it("exposes the unresolved state as a DOM attribute", () => {
    const { container } = render(
      createElement(MarkdownBody, {
        content: "see [[missing/page]]",
        id: "u1",
        remarkPlugins: [[remarkWikilink, { resolve: () => null }]],
      }),
    );

    const anchor = container.querySelector("a.wikilink--unresolved");
    expect(anchor).not.toBeNull();
    expect(anchor?.getAttribute("aria-description")).toBe(
      "Page not created yet",
    );
  });

  it("merges component overrides over the defaults", () => {
    const CustomStrong = ({ children }: { children?: ReactNode }) =>
      createElement("strong", { "data-custom": "yes" }, children);
    const { container } = render(
      createElement(MarkdownBody, {
        content: "**bold** and [link](https://example.com)",
        id: "c1",
        components: { strong: CustomStrong },
      }),
    );

    expect(container.querySelector("strong[data-custom='yes']")).not.toBeNull();
    expect(container.querySelector("a")?.className).toContain("text-accent");
  });

  it("re-renders memoized blocks when the plugin set changes", () => {
    const content = "see [[missing/page]]";
    const { container, rerender } = render(
      createElement(MarkdownBody, {
        content,
        id: "r1",
        remarkPlugins: [remarkWikilink],
      }),
    );
    expect(container.querySelector("a.wikilink")).not.toBeNull();
    expect(container.querySelector("a.wikilink--unresolved")).toBeNull();

    rerender(
      createElement(MarkdownBody, {
        content,
        id: "r1",
        remarkPlugins: [[remarkWikilink, { resolve: () => null }]],
      }),
    );
    expect(container.querySelector("a.wikilink--unresolved")).not.toBeNull();
  });
});
