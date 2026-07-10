import { describe, expect, it } from "vitest";

import {
  breadcrumbSegments,
  buildNodeIndex,
  buildPageTree,
  codePathToSourcePath,
  pageKindFromPath,
  resolveWikilinkTarget,
  wikiNodeColorVar,
  wikiNodeVal,
  type PageTreeNode,
  type WikiPageMeta,
} from "../WikiTabModel";
import { normalizePages } from "../WikiTabData";
import { pagesEnvelope } from "./fixtures";

function fixturePages(): { pages: WikiPageMeta[]; outputs: ReturnType<typeof normalizePages>["outputs"] } {
  return normalizePages(pagesEnvelope.payload);
}

function childNames(nodes: PageTreeNode[]): string[] {
  return nodes.map((node) => node.name);
}

function findChild(nodes: PageTreeNode[], name: string): PageTreeNode {
  const found = nodes.find((node) => node.name === name);
  if (!found) throw new Error(`missing tree node ${name}`);
  return found;
}

describe("pageKindFromPath", () => {
  it.each([
    ["knowledge/concepts/gobby.md", "concept"],
    ["knowledge/topics/contract-guardrails.md", "topic"],
    ["knowledge/sources/src-1234.md", "source"],
    ["recaps/2026-07-07.md", "recap"],
    ["outputs/GRAPH_REPORT.md", "output"],
    ["code/files/src/gobby/runner.py.md", "code"],
    ["code/_architecture.md", "code"],
    ["_index.md", "root"],
    ["log.md", "root"],
    ["raw/INDEX.md", "other"],
  ])("classifies %s as %s", (path, expected) => {
    expect(pageKindFromPath(path)).toBe(expected);
  });
});

describe("breadcrumbSegments", () => {
  it("returns one segment per path component with cumulative prefixes", () => {
    expect(breadcrumbSegments("knowledge/concepts/gobby.md")).toEqual([
      { label: "knowledge", prefix: "knowledge" },
      { label: "concepts", prefix: "knowledge/concepts" },
      { label: "gobby", prefix: "knowledge/concepts/gobby.md" },
    ]);
  });

  it("keeps the .md-stripped label for the leaf only", () => {
    const segments = breadcrumbSegments("code/files/src/gobby/runner.py.md");
    expect(segments[segments.length - 1]).toEqual({
      label: "runner.py",
      prefix: "code/files/src/gobby/runner.py.md",
    });
  });

  it("handles root pages", () => {
    expect(breadcrumbSegments("_index.md")).toEqual([
      { label: "_index", prefix: "_index.md" },
    ]);
  });
});

describe("codePathToSourcePath", () => {
  it("maps code/files pages back to repository paths", () => {
    expect(codePathToSourcePath("code/files/src/gobby/runner.py.md")).toBe(
      "src/gobby/runner.py",
    );
    expect(codePathToSourcePath("code/files/crates/gwiki/src/recap.rs.md")).toBe(
      "crates/gwiki/src/recap.rs",
    );
  });

  it("returns null for non code/files pages", () => {
    expect(codePathToSourcePath("code/_architecture.md")).toBeNull();
    expect(codePathToSourcePath("knowledge/concepts/gobby.md")).toBeNull();
    expect(codePathToSourcePath("code/modules/src/gobby.md")).toBeNull();
  });
});

describe("buildPageTree", () => {
  it("groups pages and outputs by path segment", () => {
    const { pages, outputs } = fixturePages();
    const tree = buildPageTree(pages, outputs);

    expect(childNames(tree)).toEqual([
      "code",
      "knowledge",
      "outputs",
      "recaps",
      "_index",
    ]);

    const knowledge = findChild(tree, "knowledge");
    expect(knowledge.kind).toBe("folder");
    expect(childNames(knowledge.children)).toEqual(["concepts", "sources", "topics"]);

    const concepts = findChild(knowledge.children, "concepts");
    expect(childNames(concepts.children)).toEqual(["gobby", "gwiki"]);
    const gobby = findChild(concepts.children, "gobby");
    expect(gobby.kind).toBe("page");
    expect(gobby.path).toBe("knowledge/concepts/gobby.md");
    expect(gobby.page?.title).toBe("Gobby");
  });

  it("sorts folders before pages at every level", () => {
    const { pages, outputs } = fixturePages();
    const tree = buildPageTree(pages, outputs);
    const kinds = tree.map((node) => node.kind);
    const firstPageIdx = kinds.indexOf("page");
    const lastFolderIdx = kinds.lastIndexOf("folder");
    expect(lastFolderIdx).toBeLessThan(firstPageIdx === -1 ? kinds.length : firstPageIdx);
  });

  it("marks outputs subtree entries as output nodes", () => {
    const { pages, outputs } = fixturePages();
    const tree = buildPageTree(pages, outputs);
    const outputsNode = findChild(tree, "outputs");
    const report = findChild(outputsNode.children, "GRAPH_REPORT");
    expect(report.kind).toBe("output");
    expect(report.output?.size).toBe(616527);
    const recovery = findChild(outputsNode.children, "recovery");
    expect(recovery.kind).toBe("folder");
    const task = findChild(recovery.children, "task-17804");
    expect(childNames(task.children)).toEqual(["post-recovery"]);
  });

  it("applies the root filter for mode-scoped trees", () => {
    const { pages, outputs } = fixturePages();
    const codeTree = buildPageTree(pages, outputs, (path) => path.startsWith("code/"));
    expect(childNames(codeTree)).toEqual(["code"]);
    const wikiTree = buildPageTree(pages, outputs, (path) => !path.startsWith("code/"));
    expect(childNames(wikiTree)).not.toContain("code");
  });
});

describe("buildNodeIndex / resolveWikilinkTarget", () => {
  it("resolves exact paths with and without the .md suffix", () => {
    const { pages } = fixturePages();
    const index = buildNodeIndex(pages);
    expect(resolveWikilinkTarget(index, "knowledge/concepts/gobby.md")).toBe(
      "knowledge/concepts/gobby.md",
    );
    expect(resolveWikilinkTarget(index, "knowledge/concepts/gobby")).toBe(
      "knowledge/concepts/gobby.md",
    );
  });

  it("resolves titles case-insensitively", () => {
    const { pages } = fixturePages();
    const index = buildNodeIndex(pages);
    expect(resolveWikilinkTarget(index, "Gobby")).toBe("knowledge/concepts/gobby.md");
    expect(resolveWikilinkTarget(index, "architecture overview")).toBe(
      "code/_architecture.md",
    );
  });

  it("resolves aliases when page metadata carries them", () => {
    const pages: WikiPageMeta[] = [
      {
        path: "knowledge/concepts/gobby.md",
        title: "Gobby",
        tags: [],
        contentHash: null,
        updatedAt: null,
        aliases: ["gobby-cli"],
      },
    ];
    const index = buildNodeIndex(pages);
    expect(resolveWikilinkTarget(index, "gobby-cli")).toBe(
      "knowledge/concepts/gobby.md",
    );
  });

  it("returns null for unresolved targets", () => {
    const { pages } = fixturePages();
    const index = buildNodeIndex(pages);
    expect(resolveWikilinkTarget(index, "knowledge/concepts/missing")).toBeNull();
    expect(resolveWikilinkTarget(index, "No Such Title")).toBeNull();
  });

  it("exposes byPath lookups keyed by exact page path", () => {
    const { pages } = fixturePages();
    const index = buildNodeIndex(pages);
    expect(index.byPath.get("knowledge/concepts/gwiki.md")?.title).toBe("Gwiki");
  });
});

describe("graph display mapping", () => {
  it("maps every graph node kind to a design token var", () => {
    expect(wikiNodeColorVar("wiki_page")).toBe("--accent");
    for (const kind of ["source", "citation", "code", "document", "unresolved_target"]) {
      expect(wikiNodeColorVar(kind)).toMatch(/^--/);
    }
  });

  it("falls back to muted for unknown kinds", () => {
    expect(wikiNodeColorVar("mystery")).toBe("--text-muted");
  });

  it("never encodes unresolved state by hue alone (uses the muted text token)", () => {
    expect(wikiNodeColorVar("unresolved_target")).toBe("--text-muted");
  });

  it("computes val = 2 + 3*sqrt(degree), clamped", () => {
    expect(wikiNodeVal(0)).toBe(2);
    expect(wikiNodeVal(1)).toBe(5);
    expect(wikiNodeVal(4)).toBe(8);
    expect(wikiNodeVal(10_000)).toBe(20);
    expect(wikiNodeVal(-3)).toBe(2);
  });
});
